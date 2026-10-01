from __future__ import annotations

import re
import time
import unicodedata
import warnings
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import pyotp
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# BS4の警告を非表示にする
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
MAX_HOPS = 15
SAML_FIELDS = {"SAMLResponse", "SAMLRequest", "RelayState"}
KULASIS_ENCODING = "cp932"  # KULASISは windows-31j(=CP932) 固定。apparent_encodingの誤判定を避ける
DEFAULT_LECTURE_SEARCH = "https://www.k.kyoto-u.ac.jp/student/la/timeslot/lecture_search"

# 再試行の設定
RETRY_STATUSES = {500, 502, 503, 504}   # Server Error / Bad Gateway / Service Unavailable / Gateway Timeout
REQ_MAX_ATTEMPTS = 5               # GETを最大何回送るか（ブラウザの再読み込み相当）。POSTは再送しない
REQ_RETRY_WAITS = (2, 3, 5, 8)     # 失敗n回目のあとに待つ秒数
APPLY_MAX_ATTEMPTS = 5             # 自動申込(candidate_add)を最大何回送るか
APPLY_RETRY_WAITS = (2, 3, 5, 8)
RETRY_BUDGET_SEC = 240             # プロセス開始からこの秒数を超えたら、以後は再試行しない
_T0 = time.monotonic()

# 実ページのページ送りリンクと同じ検索条件（hasCapacity=true が「先着順対象科目」トグル）
SEARCH_PARAMS_BASE = {
    "condition.semester": "",
    "condition.courseTitle": "",
    "condition.courseTitleEn": "",
    "condition.targetStudent": "",
    "condition.teacherName": "",
    "condition.teacherNameEn": "",
    "condition.syutyu": "false",
    "condition.hasCapacity": "true",
    "condition.numberingKateiNo": "",
    "condition.numberingDepartmentNo": "",
    "condition.numberingDisciplineNo": "",
    "condition.numberingLevelNo": "",
    "condition.numberingJugyokeitaiNo": "",
    "condition.numberingLanguageNo": "",
    "condition.numberingBunkaNo": "",
}


def budget_left() -> bool:
    """再試行に使える時間(プロセス開始から RETRY_BUDGET_SEC 秒)が残っているか。"""
    return time.monotonic() - _T0 < RETRY_BUDGET_SEC


def is_transient(e: BaseException) -> bool:
    """一時的な障害(500/502/503/504・接続エラー・タイムアウト)か。True なら再試行の価値がある。

    ID/パスワードやワンタイムパスワードの拒否(LoginError)などは False。
    """
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(e, requests.HTTPError):
        return getattr(e.response, "status_code", None) in RETRY_STATUSES
    return False


def _err_label(e: BaseException) -> str:
    status = getattr(getattr(e, "response", None), "status_code", None)
    return f"HTTP{status}" if status else type(e).__name__


def _log(tag: str, msg: str) -> None:
    """[見出し] 説明 の形式でログを出す。ID・sessid・パスワード等は出さないこと。"""
    print(f"[{tag}] {msg}")


class KulasisError(Exception):
    pass


class LoginError(KulasisError):
    pass


class MfaRequired(LoginError):
    pass


class FetchError(KulasisError):
    pass


class ApplyError(KulasisError):
    pass


def _has_password_field(html: str) -> bool:
    return BeautifulSoup(html, "lxml").find("input", {"type": "password"}) is not None


def _form_data(form) -> dict[str, str]:
    data: dict[str, str] = {}
    seen_submit = False
    # input タグに加えて button タグも対象に含める
    for inp in form.find_all(["input", "button"]):
        name = inp.get("name")
        if not name:
            continue
        t = (inp.get("type") or "text").lower()
        if t in ("reset", "image"):
            continue
        if t in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        if t == "submit":
            if seen_submit:
                continue
            seen_submit = True
        data[name] = inp.get("value", "")
    return data


def _form_summary(form) -> str:
    """認証情報の値を出さずに、画面識別に必要なフォーム構造だけを要約する。"""
    action = urlparse(form.get("action") or "").path or "(現在のURL)"
    method = (form.get("method") or "post").upper()
    fields = []
    for inp in form.find_all(["input", "button"]):
        name = inp.get("name")
        if name:
            fields.append(f"{inp.get('type') or 'text'}:{name}")
    return f"method={method}, action={action}, fields={fields}"


class KulasisClient:

    def __init__(self, cfg: dict, timeout: int = 20):
        self.cfg = cfg
        self.timeout = timeout
        self.session = requests.Session()
        self._warmed = False
        self.otp_sent = False  # ワンタイムパスワードを送信済みか（再ログイン時の待ち時間の判断に使う）
        # Accept-Language ヘッダーを追加して日本語UIを固定取得する
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ja,ja-JP;q=0.9,en;q=0.8",
        })

    def _req(self, method: str, url: str, *, retry: bool = True, **kw) -> requests.Response:
        """HTTPリクエストを送る。GETは一時的な障害(500/502/503/504等)のとき同じリクエストを再送する(再読み込み相当)。

        POSTは再送しない。サーバー側で処理済みの可能性があり、再送すると状態が壊れ得るため。
        実測: 認証画面(execution=e1s1)のPOSTが502 → 再送で500になった。
        POSTの失敗は、呼び出し側(login_with_retry / apply)が「最初からやり直す」か「反映を確認してから再送」で扱う。
        """
        attempts = REQ_MAX_ATTEMPTS if (retry and method.upper() == "GET") else 1
        for attempt in range(1, attempts + 1):
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                if not is_transient(e):
                    raise
                if attempts == 1:
                    if method.upper() != "GET":
                        _log("再送", f"{method} {urlparse(url).path}: {_err_label(e)}。POSTは二重処理の恐れがあるため再送しない")
                    raise
                if attempt >= attempts or not budget_left():
                    raise
                wait = REQ_RETRY_WAITS[min(attempt - 1, len(REQ_RETRY_WAITS) - 1)]
                _log("再送", f"{method} {urlparse(url).path}: {_err_label(e)}。{wait}秒待って再送（失敗{attempt}/{attempts}回目）")
                time.sleep(wait)
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return r

    def login(self, user: str, password: str, totp_secret: str | None = None) -> None:
        r = self._req("GET", self.cfg["login"]["start_url"])
        submitted_credentials = False
        submitted_otp = False
        current_sessid = None

        for hop in range(MAX_HOPS):
            soup = BeautifulSoup(r.text, "lxml")
            curr_url = r.url
            forms = soup.find_all("form")

            # KULASIS側に復帰し、パスワード欄が無ければログイン完了
            if "iimc.kyoto-u.ac.jp" not in curr_url and not _has_password_field(r.text):
                saml_form = None
                for form in forms:
                    names = {i.get("name") for i in form.find_all("input")}
                    if names & SAML_FIELDS:
                        saml_form = form
                        break
                if not saml_form:
                    _log("ログイン", "✅ 完了: KULASISのページに戻り、パスワード入力欄が無いことを確認")
                    return

            # Meta Refresh（自動転送）の検出
            meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
            if meta_refresh and meta_refresh.get("content"):
                content = meta_refresh["content"]
                match = re.search(r"url=['\"]?(?P<url>[^'\"]+)['\"]?", content, re.I)
                if match:
                    redirect_url = urljoin(r.url, match.group("url"))
                    _log("ログイン", f"自動転送(Meta Refresh)に従って移動: {urlparse(redirect_url).path}")
                    r = self._req("GET", redirect_url)
                    continue

            # 1. SAML（Security Assertion Markup Language：異なるシステム間で認証情報を安全に交換する規格）中継フォームの自動送信
            saml_form = None
            for form in forms:
                names = {i.get("name") for i in form.find_all("input")}
                if names & SAML_FIELDS:
                    saml_form = form
                    break
            if saml_form:
                _log("ログイン", "SAML中継フォームを自動送信（KULASISと統合認証の間で認証結果を受け渡し）")
                action = urljoin(r.url, saml_form.get("action") or r.url)
                data = _form_data(saml_form)
                r = self._req("POST", action, data=data)
                continue

            # 2. 初回 ID / パスワード入力画面 (login.cgi)
            pw_input = soup.find("input", {"type": "password"})
            if pw_input is not None:
                if submitted_credentials:
                    raise LoginError("ID/パスワードが受理されなかった(認証画面に戻された)")
                form = pw_input.find_parent("form")
                data = _form_data(form)
                if data.get("sessid"):
                    current_sessid = data["sessid"]

                user_field = next(
                    (
                        i.get("name")
                        for i in form.find_all("input")
                        if i.get("name")
                        and i.get("name") != "dummy"
                        and (i.get("type") or "text").lower() in ("text", "email")
                    ),
                    None,
                )
                if not user_field or not pw_input.get("name"):
                    raise LoginError("ログインフォームの入力欄名を特定できなかった")
                data[user_field] = user
                data[pw_input["name"]] = password
                submitted_credentials = True

                # ID・パスワード・sessid は Actions のログに残さない（入力欄の名前だけ出す）
                _log("ログイン", f"ID/パスワードを送信（入力欄: {user_field} / {pw_input['name']}）")

                action = urljoin(r.url, form.get("action") or r.url)
                method = (form.get("method") or "post").upper()
                kw = {"params": data} if method == "GET" else {"data": data}
                r = self._req(method, action, **kw)
                continue

            # 3. FIDO/U2F 画面 ➔ authselect.php への遷移
            u2f_form = soup.find("form", action=re.compile(r"u2flogin", re.I))
            auth_link = soup.find("a", href=re.compile(r"authselect\.php", re.I))
            auth_form = soup.find("form", action=re.compile(r"authselect\.php", re.I))

            if ("u2flogin" in curr_url or u2f_form or auth_link or auth_form) and "authselect.php" not in curr_url:
                _log("ログイン", "セキュリティキー(FIDO/U2F)の認証画面を検出。認証方式の選択画面へ切替")
                if auth_link and auth_link.get("href"):
                    target_url = urljoin(r.url, auth_link["href"])
                    r = self._req("GET", target_url)
                    continue

                target_form = auth_form or u2f_form
                if target_form:
                    action = urljoin(r.url, target_form.get("action") or r.url)
                    method = (target_form.get("method") or "post").upper()
                    data = _form_data(target_form)
                    kw = {"params": data} if method == "GET" else {"data": data}
                    r = self._req("POST" if method == "POST" else "GET", action, **kw)
                    continue

            # 4. 認証方式選択画面 (authselect.php) ➔ otplogin.cgi への遷移構築
            if "authselect.php" in curr_url:
                _log("ログイン", "認証方式の選択画面を検出。ワンタイムパスワード(TOTP)のページへ移動")

                parsed = urlparse(curr_url)
                qs = parse_qs(parsed.query)

                if current_sessid and (not qs.get("sessid") or not qs["sessid"][0]):
                    qs["sessid"] = [current_sessid]

                flat_qs = {k: v[0] for k, v in qs.items()}
                flat_qs["method"] = ""
                flat_qs["excluded"] = "u2flogin,fidouplogin,fidouvlogin,otplogin"

                new_query = urlencode(flat_qs)
                otplogin_url = urlunparse((parsed.scheme, parsed.netloc, "/pub/otplogin.cgi", parsed.params, new_query, parsed.fragment))

                r = self._req("GET", otplogin_url)
                continue

            # 5. OTP入力画面 (otplogin.cgi)
            is_otp_page = (
                "otplogin" in curr_url
                or soup.find("input", {"id": "password_input"})
                or soup.find("input", {"class": "onetime_input"})
            )
            if is_otp_page:
                otp_input = (
                    soup.find("input", {"id": "password_input"})
                    or soup.find("input", {"class": "onetime_input"})
                    or soup.find("input", {"id": re.compile(r"otp", re.I)})
                    or soup.find("input", {"name": re.compile(r"otp", re.I)})
                )
                if otp_input is not None:
                    if not totp_secret:
                        raise MfaRequired("TOTP_SECRETが未設定のため、2段階認証を通過できない")
                    if submitted_otp:
                        raise LoginError("ワンタイムパスワードが拒否された")

                    form = otp_input.find_parent("form")
                    data = _form_data(form)
                    data.pop("dummy", None)

                    clean_secret = totp_secret.strip().replace(" ", "").upper()
                    otp_code = pyotp.TOTP(clean_secret).now()
                    data[otp_input["name"]] = otp_code
                    submitted_otp = True
                    self.otp_sent = True

                    if current_sessid and not data.get("sessid"):
                        data["sessid"] = current_sessid

                    _log("ログイン", f"ワンタイムパスワード(TOTP)を送信（入力欄: {otp_input.get('name')}）")

                    action = urljoin(r.url, form.get("action") or r.url)
                    method = (form.get("method") or "post").upper()
                    kw = {"params": data} if method == "GET" else {"data": data}
                    r = self._req("POST" if method == "POST" else "GET", action, retry=False, **kw)
                    continue

            # 単一フォームの自動送信フォールバック
            if len(forms) == 1:
                form = forms[0]
                title = soup.title.get_text(" ", strip=True) if soup.title else "(なし)"
                _log(
                    "認証診断",
                    "未分類の単一フォームを自動送信: "
                    f"URL={urlparse(curr_url).path}, title={title!r}, {_form_summary(form)}",
                )
                action = urljoin(r.url, form.get("action") or r.url)
                method = (form.get("method") or "post").upper()
                data = _form_data(form)
                kw = {"params": data} if method == "GET" else {"data": data}
                r = self._req("POST" if method == "POST" else "GET", action, **kw)
                continue

            form_actions = [f.get("action") for f in forms]
            links = [a.get("href") for a in soup.find_all("a")]
            raise LoginError(f"未対応の認証ステップに到達した (URL: {curr_url}, フォーム動作先: {form_actions}, リンク先: {links[:3]})")

        raise LoginError(f"リダイレクト/フォーム送信が{MAX_HOPS}回を超えた")

    # ------------------------------------------------------------------
    # 履修登録ページ: 科目検索（先着順）と追加
    # ------------------------------------------------------------------
    def _lecture_search_url(self) -> str:
        return self.cfg.get("pages", {}).get("lecture_search", DEFAULT_LECTURE_SEARCH)

    def _sibling(self, name: str) -> str:
        """lecture_search と同じ階層(/student/la/timeslot/)のURLを作る。"""
        return urljoin(self._lecture_search_url(), name)

    def _warmup(self) -> None:
        """ブラウザの遷移(履修登録トップ → 時間割ページ)を再現する。失敗しても続行。"""
        if self._warmed:
            return
        self._warmed = True
        for page in ("top", "timeslot_list"):
            try:
                self._req("GET", self._sibling(page))
            except requests.RequestException as e:
                _log("準備", f"{page} ページの取得に失敗。続行する: {e}")

    def fetch_lecture_search(self, course_title: str, max_pages: int = 3) -> list[str]:
        """先着順対象科目を科目名で検索し、結果ページのHTMLを返す（1ページ30件、最大max_pagesまで）。

        日本語の検索語はcp932でパーセントエンコードして送る（実ページのリンクと同じ方式）。
        ローマ数字(Ⅱ)はNFKCでアルファベット(II)へ変換（サイトの注意書きに従う）。
        """
        self._warmup()
        base = self._lecture_search_url()
        title = unicodedata.normalize("NFKC", course_title).strip()
        pages: list[str] = []
        for page in range(1, max_pages + 1):
            _log("検索", f"科目名「{title}」を先着順対象科目から検索（{page}ページ目）")
            params = {**SEARCH_PARAMS_BASE, "condition.courseTitle": title, "page": str(page)}
            query = urlencode(params, encoding=KULASIS_ENCODING, errors="replace")
            r = self._req("GET", f"{base}?{query}")
            r.encoding = KULASIS_ENCODING
            pages.append(r.text)
            soup = BeautifulSoup(r.text, "lxml")
            if soup.find("a", string=re.compile("次の30件")) is None:
                break
        return pages

    @staticmethod
    def _registered_nos(html: str) -> set[str]:
        """時間割ページに載っている全学共通科目の lectureNo 一覧。

        科目名は「全共:Biologi..」のように省略表示されるため、科目詳細リンク
        /student/la/support/top?no=<lectureNo> の番号で判定する（実ページで一致を確認済み）。
        """
        return set(re.findall(r"/student/la/support/top\?no=(\d+)", html))

    def _timeslot_html(self) -> str:
        r = self._req("GET", self._sibling("timeslot_list"))
        r.encoding = KULASIS_ENCODING
        return r.text

    def apply(self, lecture_no: str) -> bool:
        """指定lectureNoの科目を「候補科目」に追加する(candidate_add)。

        戻り値: True = 追加を送信して時間割への反映を確認した / False = 既に時間割にあり送信しなかった
        実測: POST candidate_add → 302 → timeslot_list（確認ページなし）。
        一時的な障害(502等)では最大 APPLY_MAX_ATTEMPTS 回まで送り直す。
        ただし502でもサーバー側で処理済みの場合があるため、再送の前に時間割で反映を確認する。
        ※履修登録の確定は登録期間(Step2)の「登録科目の決定へ」で別途行う必要がある。
        """
        # 二重追加時の挙動が未確認なので、既に時間割に入っていれば送信しない
        if lecture_no in self._registered_nos(self._timeslot_html()):
            _log("申込", f"lectureNo={lecture_no} は既に時間割(候補)にあるため、追加は送信しない")
            return False

        for attempt in range(1, APPLY_MAX_ATTEMPTS + 1):
            try:
                return self._apply_once(lecture_no)
            except requests.RequestException as e:
                if not is_transient(e) or attempt >= APPLY_MAX_ATTEMPTS or not budget_left():
                    raise
                wait = APPLY_RETRY_WAITS[min(attempt - 1, len(APPLY_RETRY_WAITS) - 1)]
                _log("申込", f"送信失敗 {_err_label(e)}。{wait}秒待って再送（失敗{attempt}/{APPLY_MAX_ATTEMPTS}回目）")
                time.sleep(wait)
                try:
                    if lecture_no in self._registered_nos(self._timeslot_html()):
                        _log("申込", "時間割への反映を確認。再送は不要")
                        return True
                except requests.RequestException as e2:
                    _log("申込", f"時間割の確認に失敗: {_err_label(e2)}。そのまま再送する")
        raise AssertionError("unreachable")

    def _apply_once(self, lecture_no: str) -> bool:
        r = self._req("POST", self._sibling("candidate_add"), retry=False, data={"lectureNo": lecture_no})
        r.encoding = KULASIS_ENCODING

        chain = " -> ".join(f"{h.status_code} {h.headers.get('Location', '')}" for h in r.history)
        _log("申込", f"「追加」を送信 lectureNo={lecture_no}（応答経路: {chain or 'リダイレクトなし'} / 最終ページ: {r.url}）")

        if _has_password_field(r.text):
            raise ApplyError(f"セッション切れの可能性: ログイン画面に戻された lectureNo={lecture_no}")

        # リダイレクト先が timeslot_list ならその応答で確認、違えば取り直す
        after = r.text if "timeslot_list" in r.url else self._timeslot_html()
        ok = lecture_no in self._registered_nos(after)
        _log("申込", f"時間割ページで科目を確認: {'✅ 載っている（追加成功）' if ok else '❌ 見当たらない（追加失敗の可能性）'}")
        if not ok:
            area = BeautifulSoup(after, "lxml").find("div", class_="contents")
            snippet = " ".join((area or BeautifulSoup(after, "lxml")).get_text(" ", strip=True).split())[:400]
            _log("申込", f"応答本文(先頭400字): {snippet}")
            raise ApplyError(f"追加後の時間割に科目が見当たらない(追加失敗の可能性) lectureNo={lecture_no}")
        return True