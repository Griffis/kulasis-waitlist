from __future__ import annotations

import re
from urllib.parse import urljoin

import pyotp
import requests
from bs4 import BeautifulSoup
import warnings
from bs4 import XMLParsedAsHTMLWarning

# BS4の警告を非表示にする
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
MAX_HOPS = 15
SAML_FIELDS = {"SAMLResponse", "SAMLRequest", "RelayState"}
KULASIS_ENCODING = "cp932"  # KULASISは windows-31j(=CP932) 固定。apparent_encodingの誤判定を避ける


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

class KulasisClient:

    def __init__(self, cfg: dict, timeout: int = 20):
        self.cfg = cfg
        self.timeout = timeout
        self.session = requests.Session()
        # Accept-Language ヘッダーを追加して日本語UIを固定取得する
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ja,ja-JP;q=0.9,en;q=0.8",
        })

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        r = self.session.request(method, url, timeout=self.timeout, **kw)
        r.raise_for_status()
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
            title = soup.title.string.strip() if soup.title and soup.title.string else "No Title"
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
                    print("[DEBUG] ログイン完了を検出しました")
                    return

            # Meta Refresh（自動転送）の検出
            meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
            if meta_refresh and meta_refresh.get("content"):
                content = meta_refresh["content"]
                match = re.search(r"url=['\"]?(?P<url>[^'\"]+)['\"]?", content, re.I)
                if match:
                    redirect_url = urljoin(r.url, match.group("url"))
                    print(f" -> Meta Refresh転送: {redirect_url}")
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
                print(" -> SAMLフォーム自動送信")
                action = urljoin(r.url, saml_form.get("action") or r.url)
                data = _form_data(saml_form)
                r = self._req("POST", action, data=data)
                continue

            # 2. 初回 ID / パスワード入力画面 (login.cgi)
            pw_input = soup.find("input", {"type": "password"})
            if pw_input is not None:
                if submitted_credentials:
                    raise LoginError("ID/パスワードが受け付けられませんでした(認証画面に戻されました)")
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
                    raise LoginError("ログインフォームの入力欄名を特定できませんでした")
                data[user_field] = user
                data[pw_input["name"]] = password
                submitted_credentials = True

                masked_data = {k: ("***" if "pass" in k.lower() else v) for k, v in data.items()}
                print(f" -> ID/パスワード送信 [ユーザー欄: {user_field}='{user}', 送信データ: {masked_data}]")

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
                print(" -> FIDO画面回避 (authselectへ遷移)")
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
                print(" -> authselect.php を検出")
                from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

                parsed = urlparse(curr_url)
                qs = parse_qs(parsed.query)

                if current_sessid and (not qs.get("sessid") or not qs["sessid"][0]):
                    qs["sessid"] = [current_sessid]

                flat_qs = {k: v[0] for k, v in qs.items()}
                flat_qs["method"] = ""
                flat_qs["excluded"] = "u2flogin,fidouplogin,fidouvlogin,otplogin"

                new_query = urlencode(flat_qs)
                otplogin_url = urlunparse((parsed.scheme, parsed.netloc, "/pub/otplogin.cgi", parsed.params, new_query, parsed.fragment))

                print(f" -> otplogin.cgi へ遷移補完: {otplogin_url}")
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
                        raise MfaRequired("TOTP_SECRETが設定されていないため、2段階認証を突破できません")
                    if submitted_otp:
                        raise LoginError("ワンタイムパスワードが拒否されました")

                    form = otp_input.find_parent("form")
                    data = _form_data(form)
                    data.pop("dummy", None)

                    clean_secret = totp_secret.strip().replace(" ", "").upper()
                    otp_code = pyotp.TOTP(clean_secret).now()
                    data[otp_input["name"]] = otp_code
                    submitted_otp = True

                    if current_sessid and not data.get("sessid"):
                        data["sessid"] = current_sessid

                    print(f" -> OTPコード送信 [入力欄: {otp_input.get('name')}]")

                    action = urljoin(r.url, form.get("action") or r.url)
                    method = (form.get("method") or "post").upper()
                    kw = {"params": data} if method == "GET" else {"data": data}
                    r = self._req("POST" if method == "POST" else "GET", action, **kw)
                    continue

            # 単一フォームの自動送信フォールバック
            if len(forms) == 1:
                print(" -> 単一フォーム自動送信")
                form = forms[0]
                action = urljoin(r.url, form.get("action") or r.url)
                method = (form.get("method") or "post").upper()
                data = _form_data(form)
                kw = {"params": data} if method == "GET" else {"data": data}
                r = self._req("POST" if method == "POST" else "GET", action, **kw)
                continue

            form_actions = [f.get("action") for f in forms]
            links = [a.get("href") for a in soup.find_all("a")]
            raise LoginError(f"未対応の認証ステップに到達しました (URL: {curr_url}, フォーム動作先: {form_actions}, リンク先: {links[:3]})")

        raise LoginError(f"リダイレクト/フォーム送信が{MAX_HOPS}回を超えました")

    def fetch_entrylimit_page(self) -> str:
        """履修(人数)制限ページのHTMLを取得する"""
        url = self.cfg.get("pages", {}).get(
            "entrylimit", "https://www.k.kyoto-u.ac.jp/student/la/entrylimit/regist"
        )
        r = self._req("GET", url)
        r.encoding = KULASIS_ENCODING
        return r.text

    def apply(self, lecture_no: str) -> None:
        """指定lecture_noの科目に申込む。

        規約: 申込ボタン(regist_check) → 確認画面(primaryId) → 確定(regist_save)
              の2段階。成功判定は最終到達URLに regist_complete が含まれるかで行う。
        """
        entrylimit_url = self.cfg.get("pages", {}).get(
            "entrylimit", "https://www.k.kyoto-u.ac.jp/student/la/entrylimit/regist"
        )

        # 1段階目: 申込 → 確認画面
        r1 = self._req(
            "POST",
            urljoin(entrylimit_url, "regist_check"),
            data={"entrylimitLectureNo": lecture_no},
        )
        r1.encoding = KULASIS_ENCODING
        soup = BeautifulSoup(r1.text, "lxml")

        primary_id_input = soup.find("input", {"name": "primaryId"})
        confirm_form = primary_id_input.find_parent("form") if primary_id_input else None
        if not confirm_form:
            raise ApplyError(
                f"確認画面(primaryId)が見つかりません。lecture_no={lecture_no} 到達URL={r1.url}"
            )

        # 2段階目: 確認 → 確定
        action = urljoin(r1.url, confirm_form.get("action") or "regist_save")
        data = _form_data(confirm_form)
        r2 = self._req("POST", action, data=data)
        r2.encoding = KULASIS_ENCODING

        if "regist_complete" not in r2.url:
            raise ApplyError(
                f"申込完了を確認できませんでした。lecture_no={lecture_no} 最終到達URL={r2.url}"
            )