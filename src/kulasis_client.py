from __future__ import annotations

import random
import re
import time
import unicodedata
import warnings
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import pyotp
import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from .config import PROFILES, Profile

# BS4の警告を非表示にする
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
MAX_HOPS = 15
SAML_FIELDS = {"SAMLResponse", "SAMLRequest", "RelayState"}
KULASIS_ENCODING = "cp932"  # KULASISは windows-31j(=CP932) 固定。apparent_encodingの誤判定を避ける
DEFAULT_LECTURE_SEARCH = "https://www.k.kyoto-u.ac.jp/student/la/timeslot/lecture_search"

# 待ち時間・再試行の設定
# 根拠: 履修登録ページ(/student/la/timeslot/)は、混雑時に「順番に処理」され「10分程度かかることがある」
#       「戻る・更新を押さず待つ」と画面に表示される(ページ内のJavaScript)。実際に read timeout=20 の失敗も記録されている。
#       そのため、このパスの読み取り待ちは長くし、短い間隔での連打(再読み込み)は避ける。
READ_TIMEOUT_SEC = 20                  # 通常ページ(統合認証など)の読み取り待ち
TIMESLOT_PATH = "/student/la/timeslot/"   # この配下の読み取り待ちはモード(Profile)で決まる
CONNECT_TIMEOUT_SEC = 5                # 接続できないサーバーを長く待たない
SLOW_REQUEST_LOG_SEC = 3               # これ以上かかった応答は「遅延」としてログに出す（待ち時間の調整材料）
RETRY_STATUS_EXTRA = {408, 425, 429}   # Request Timeout / Too Early / Too Many Requests
NON_RETRY_5XX = {501, 505, 511}        # 未実装 / HTTP版非対応 / ネットワーク認証要 → 繰り返しても結果は同じ
REQ_MAX_ATTEMPTS = 5                   # GETを最大何回送るか。POSTは再送しない
RETRY_WAITS = (1, 2, 4, 8)             # GET再送: 失敗n回目のあとに待つ秒数（±20%のゆらぎ付き）
REQ_RETRY_WAITS = RETRY_WAITS
APPLY_MAX_ATTEMPTS = 5                 # 自動申込(candidate_add)を最大何回送るか
APPLY_RETRY_WAITS = (0.5, 1, 2, 3)     # 申込は席の争奪なので、他より短め
APPLY_SETTLE_WAITS = (3, 6)            # 応答に時間がかかった失敗のあと、再送の前に反映を確認し直す間隔（元のリクエストが処理中の恐れ）
APPLY_SLOW_FAIL_SEC = 5                # この秒数以上かかってから失敗したものを「応答に時間がかかった失敗」とみなす
APPLY_CONFIRM_RECHECKS = 2             # 追加送信後に時間割へ反映されていないとき、確認し直す回数
APPLY_CONFIRM_WAIT = 0.5               # その待ち秒数
TIMESLOT_CACHE_MAX_AGE_SEC = 120       # 事前取得した時間割ページを、申込前の確認に使える秒数（通信は増やさない）
RETRY_AFTER_CAP_SEC = 20               # Retry-After ヘッダの待ち秒数の上限
RATE_LIMIT_MIN_WAIT = 2.0              # 429でRetry-Afterが無いときの最短待ち（連打でアクセス制限を悪化させない）
RETRY_BUDGET_SEC = 360                 # プロセス開始からこの秒数を超えたら、以後は再試行しない
RUN_DEADLINE_SEC = 480                 # 1回の実行の上限。各リクエストの待ちもここまでに収める（Actionsのtimeout=10分）
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


# 通信の途中切断・圧縮データ破損なども含め、再送すれば直る可能性がある例外
TRANSIENT_EXC = (
    requests.ConnectionError,                    # 接続失敗・切断・プロキシ・SSL（ConnectTimeoutも含む）
    requests.Timeout,                            # 読み取りタイムアウト
    requests.exceptions.ChunkedEncodingError,    # レスポンス受信の途中で切断
    requests.exceptions.ContentDecodingError,    # 圧縮データが壊れた
)


def is_retryable_status(code: int | None) -> bool:
    """HTTPステータスが「再送すれば直る可能性がある」ものか（408/425/429と、501・505・511以外の5xx）。"""
    if code is None:
        return False
    return code in RETRY_STATUS_EXTRA or (500 <= code <= 599 and code not in NON_RETRY_5XX)


def is_transient(e: BaseException) -> bool:
    """一時的な障害か。True なら再試行の価値がある。

    再試行しないもの: ID/パスワード・ワンタイムパスワードの拒否(LoginError)、400/401/403/404などの4xx、
    501/505/511、不正なURL、リダイレクトの無限ループ。繰り返しても同じ結果になり、ロックの恐れもあるため。
    """
    if isinstance(e, TRANSIENT_EXC):
        return True
    if isinstance(e, requests.HTTPError):
        return is_retryable_status(getattr(e.response, "status_code", None))
    return isinstance(e, TransientError)


def _parse_retry_after(headers) -> float | None:
    """Retry-After ヘッダ（秒数 または HTTP日付）を秒数にする。無い/解釈できなければ None。"""
    try:
        value = (headers or {}).get("Retry-After")
    except AttributeError:
        return None
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (dt - datetime.now(dt.tzinfo or timezone.utc)).total_seconds())


def _retry_wait(attempt: int, e: BaseException | None = None, waits: tuple = RETRY_WAITS) -> float:
    """失敗 attempt 回目のあとに待つ秒数。間隔を倍々に広げ、サーバーがRetry-Afterを返したらそれに従う。"""
    base = waits[min(attempt - 1, len(waits) - 1)]
    response = getattr(e, "response", None)
    status = getattr(response, "status_code", None)
    if status in (429, 503):
        retry_after = _parse_retry_after(getattr(response, "headers", None))
        if retry_after is not None:
            return min(retry_after, RETRY_AFTER_CAP_SEC)
    if status == 429:
        base = max(base, RATE_LIMIT_MIN_WAIT)
    return base * random.uniform(0.8, 1.2)


def _err_label(e: BaseException) -> str:
    status = getattr(getattr(e, "response", None), "status_code", None)
    return f"HTTP{status}" if status else type(e).__name__


def _log(tag: str, msg: str) -> None:
    """[見出し] 説明 の形式でログを出す。ID・sessid・パスワード等は出さないこと。"""
    print(f"[{tag}] {msg}")


class KulasisError(Exception):
    pass


class TransientError(KulasisError):
    """再試行すれば直る可能性がある失敗（メンテナンス画面など、HTTP 200でも中身が想定外の場合を含む）。"""


class LoginError(KulasisError):
    pass


class MfaRequired(LoginError):
    pass


class FetchError(KulasisError):
    pass


class UnexpectedPage(TransientError):
    """HTTPは成功したが、ページの中身が想定と違う（メンテナンス画面・エラー画面など）。"""


class UnexpectedLoginStep(UnexpectedPage, LoginError):
    """ログインの途中で、想定外の画面に到達した。"""


class ApplyError(KulasisError):
    pass


def _looks_like_search_page(html: str) -> bool:
    """検索結果ページとして想定どおりか。メンテナンス画面・エラー画面(HTTP 200)などを弾く。"""
    return "検索結果は全部で" in html or "subheading_search_result" in html


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

    def __init__(self, cfg: dict, timeout: int = READ_TIMEOUT_SEC, profile: Profile | None = None):
        self.cfg = cfg
        self.profile = profile or PROFILES["watch"]  # 動作モード（事前取得の有無・待ち時間）
        self.timeout = timeout
        self.session = requests.Session()
        self._warmed = False
        self.otp_sent = False  # ワンタイムパスワードを送信済みか（再ログイン時の待ち時間の判断に使う）
        self._timeslot_cache: tuple[float, str] | None = None  # (取得時刻, 時間割ページHTML)
        # Accept-Language ヘッダーを追加して日本語UIを固定取得する
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ja,ja-JP;q=0.9,en;q=0.8",
        })

    def _read_timeout(self, url: str) -> float:
        """URLに応じた読み取り待ち秒数。履修登録ページは混雑時に応答が遅いので長く待つ。実行の上限も超えない。"""
        base = self.profile.timeslot_read_timeout_sec if TIMESLOT_PATH in url else self.timeout
        remaining = RUN_DEADLINE_SEC - (time.monotonic() - _T0)
        return min(float(base), max(5.0, remaining))  # 実行上限が近くても最低5秒は待つ

    def _req(self, method: str, url: str, *, retry: bool = True, **kw) -> requests.Response:
        """HTTPリクエストを送る。GETは一時的な障害のとき、間隔を空けて同じリクエストを再送する(再読み込み相当)。

        POSTは再送しない。サーバー側で処理済みの可能性があり、再送すると状態が壊れ得るため。
        実測: 認証画面(execution=e1s1)のPOSTが502 → 再送で500になった。
        POSTの失敗は、呼び出し側(login_with_retry / apply)が「最初からやり直す」か「反映を確認してから再送」で扱う。
        """
        attempts = REQ_MAX_ATTEMPTS if (retry and method.upper() == "GET") else 1
        path = urlparse(url).path
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                r = self.session.request(method, url, timeout=(CONNECT_TIMEOUT_SEC, self._read_timeout(url)), **kw)
                elapsed = time.monotonic() - started
                if elapsed >= SLOW_REQUEST_LOG_SEC:
                    _log("遅延", f"{method} {path} の応答に{elapsed:.1f}秒かかった（HTTP{r.status_code}）")
                r.raise_for_status()
                break
            except requests.RequestException as e:
                if not is_transient(e):
                    raise
                if attempts == 1:
                    if method.upper() != "GET":
                        _log("再送", f"{method} {path}: {_err_label(e)}。POSTは二重処理の恐れがあるため再送しない")
                    raise
                if attempt >= attempts or not budget_left():
                    raise
                wait = _retry_wait(attempt, e)
                waited = time.monotonic() - started
                _log("再送", f"{method} {path}: {_err_label(e)}（{waited:.1f}秒後に失敗）。{wait:.1f}秒待って再送（失敗{attempt}/{attempts}回目）")
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
            raise UnexpectedLoginStep(f"未対応の認証ステップに到達した (URL: {curr_url}, フォーム動作先: {form_actions}, リンク先: {links[:3]})")

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
        """事前取得(warmup)。検索の前に、履修登録トップ → 時間割ページを1回ずつ開く。失敗しても続行。

        warmup は warm up（運動前の準備運動）から来た語で、本番の処理の前に行う下準備のこと。
        ここでは「ブラウザの遷移(トップ → 時間割 → 検索)を再現する」ために行う。
        必須かどうかは未確認。動作モード(Profile.warmup)で省略でき、混雑時(rush)は省略して遷移を減らす。
        取得した時間割ページは、申込前の「既に入っているか」の確認に使い回す（追加の通信はしない）。
        """
        if self._warmed:
            return
        self._warmed = True
        if not self.profile.warmup:
            _log("準備", f"モード {self.profile.name}: 事前取得(履修登録トップ・時間割ページ)を省略")
            return
        for page in ("top", "timeslot_list"):
            try:
                if page == "timeslot_list":
                    self._timeslot_html()
                else:
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
            for attempt in range(1, REQ_MAX_ATTEMPTS + 1):
                r = self._req("GET", f"{base}?{query}")
                r.encoding = KULASIS_ENCODING
                if _looks_like_search_page(r.text):
                    break
                if attempt >= REQ_MAX_ATTEMPTS or not budget_left():
                    raise UnexpectedPage("検索結果ページの形式が想定と異なる(メンテナンス・エラー画面、セッション切れ等の可能性)")
                wait = _retry_wait(attempt)
                _log("再送", f"検索ページの内容が想定外(HTTP200)。{wait:.1f}秒待って再送（失敗{attempt}/{REQ_MAX_ATTEMPTS}回目）")
                time.sleep(wait)
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
        """時間割ページのHTMLを取得する（取得結果は申込前の確認用に保持する）。"""
        r = self._req("GET", self._sibling("timeslot_list"))
        r.encoding = KULASIS_ENCODING
        self._timeslot_cache = (time.monotonic(), r.text)
        return r.text

    def _cached_timeslot_html(self, max_age: float) -> str | None:
        """max_age 秒以内に取得済みの時間割ページ。無ければ None（通信はしない）。"""
        if self._timeslot_cache:
            fetched_at, html = self._timeslot_cache
            if time.monotonic() - fetched_at <= max_age:
                return html
        return None

    def apply(self, lecture_no: str) -> bool:
        """指定lectureNoの科目を「候補科目」に追加する(candidate_add)。

        戻り値: True = 追加を送信して時間割への反映を確認した / False = 既に時間割にあり送信しなかった
        実測: POST candidate_add → 302 → timeslot_list（確認ページなし）。成否は、この応答(時間割ページ)で確認する。
        一時的な障害(502等)では最大 APPLY_MAX_ATTEMPTS 回まで送り直す。
        ただし502でもサーバー側で処理済み・処理中の場合があるため、再送の前に時間割で反映を確認する。
        ※履修登録の確定は登録期間(Step2)の「登録科目の決定へ」で別途行う必要がある。
        """
        # 申込前の確認は、事前取得した時間割ページがあるときだけ行う（追加の通信は増やさない）
        cached = self._cached_timeslot_html(TIMESLOT_CACHE_MAX_AGE_SEC)
        if cached is not None and lecture_no in self._registered_nos(cached):
            _log("申込", f"lectureNo={lecture_no} は既に時間割(候補)にあるため、追加は送信しない")
            return False

        for attempt in range(1, APPLY_MAX_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                return self._apply_once(lecture_no)
            except requests.RequestException as e:
                if not is_transient(e) or attempt >= APPLY_MAX_ATTEMPTS or not budget_left():
                    raise
                elapsed = time.monotonic() - started
                wait = _retry_wait(attempt, e, APPLY_RETRY_WAITS)
                _log("申込", f"送信失敗 {_err_label(e)}（{elapsed:.1f}秒後）。{wait:.1f}秒待って、反映を確認してから再送（失敗{attempt}/{APPLY_MAX_ATTEMPTS}回目）")
                time.sleep(wait)
                # 応答に時間がかかった失敗は、元のリクエストがまだ処理中の恐れがある。確認を間隔を空けて重ねてから再送する
                checks = 1 + (len(APPLY_SETTLE_WAITS) if elapsed >= APPLY_SLOW_FAIL_SEC else 0)
                for i in range(checks):
                    if i:
                        time.sleep(APPLY_SETTLE_WAITS[i - 1])
                    try:
                        if lecture_no in self._registered_nos(self._timeslot_html()):
                            _log("申込", "時間割への反映を確認。再送は不要")
                            return True
                    except requests.RequestException as e2:
                        _log("申込", f"時間割の確認に失敗: {_err_label(e2)}")
        raise AssertionError("unreachable")

    def _apply_once(self, lecture_no: str) -> bool:
        self._timeslot_cache = None  # 追加を送ると時間割が変わるので、キャッシュは捨てる
        r = self._req("POST", self._sibling("candidate_add"), retry=False, data={"lectureNo": lecture_no})
        r.encoding = KULASIS_ENCODING

        chain = " -> ".join(f"{h.status_code} {h.headers.get('Location', '')}" for h in r.history)
        _log("申込", f"「追加」を送信 lectureNo={lecture_no}（応答経路: {chain or 'リダイレクトなし'} / 最終ページ: {r.url}）")

        if _has_password_field(r.text):
            raise ApplyError(f"セッション切れの可能性: ログイン画面に戻された lectureNo={lecture_no}")

        # リダイレクト先が timeslot_list ならその応答で確認、違えば取り直す
        after = r.text if "timeslot_list" in r.url else self._timeslot_html()
        ok = lecture_no in self._registered_nos(after)
        for _ in range(APPLY_CONFIRM_RECHECKS):  # 反映が遅れる場合に備え、短い間隔で確認し直す
            if ok:
                break
            time.sleep(APPLY_CONFIRM_WAIT)
            ok = lecture_no in self._registered_nos(self._timeslot_html())
        _log("申込", f"時間割ページで科目を確認: {'✅ 載っている（追加成功）' if ok else '❌ 見当たらない（追加失敗の可能性）'}")
        if not ok:
            area = BeautifulSoup(after, "lxml").find("div", class_="contents")
            snippet = " ".join((area or BeautifulSoup(after, "lxml")).get_text(" ", strip=True).split())[:400]
            _log("申込", f"応答本文(先頭400字): {snippet}")
            raise ApplyError(f"追加後の時間割に科目が見当たらない(追加失敗の可能性) lectureNo={lecture_no}")
        return True
    # ------------------------------------------------------------------
    # 検証用
    # ------------------------------------------------------------------
    @staticmethod
    def _notice_phrases(html: str) -> set[str]:
        """ページ内の、エラー・重複を示しそうな文言を集める（応答の差分を見る用）。"""
        text = " ".join(BeautifulSoup(html, "lxml").get_text(" ", strip=True).split())
        return set(re.findall(r"[^。 ]{0,30}(?:エラー|既に|すでに|重複|できません|失敗)[^。 ]{0,30}", text))

    def debug_double_apply(self, lecture_no: str) -> None:
        """【検証用】candidate_add を続けて2回送り、重複したときのサーバーの応答を観察する。実際に追加される。

        見るもの: 2回目の応答経路、時間割での科目の出現数が増えるか（重複登録されるか）、2回目だけに出る注意文言。
        """
        marker = f"/student/la/support/top?no={lecture_no}"
        _log("検証", f"lectureNo={lecture_no} の追加を続けて2回送る（実際に追加される。後で時間割から削除すること）")
        counts: list[int] = []
        first_notices: set[str] = set()
        for i in (1, 2):
            r = self._req("POST", self._sibling("candidate_add"), retry=False, data={"lectureNo": lecture_no})
            r.encoding = KULASIS_ENCODING
            chain = " -> ".join(f"{h.status_code} {h.headers.get('Location', '')}" for h in r.history)
            counts.append(r.text.count(marker))
            notices = self._notice_phrases(r.text)
            if i == 1:
                first_notices = notices
                extra = "(基準)"
            else:
                extra = sorted(notices - first_notices) or "差分なし"
            _log("検証", f"{i}回目: 経路={chain or 'なし'} / 最終ページ={r.url} / 時間割での出現数={counts[-1]} / 1回目との文言の差={extra}")
        if counts[1] > counts[0]:
            _log("検証", "結果: 2回目で出現数が増えた（重複して登録された）。時間割から余分な1件を削除すること")
        else:
            _log("検証", "結果: 出現数は増えていない（重複登録されていない）")