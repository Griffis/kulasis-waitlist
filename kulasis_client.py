"""KULASISへのログインと「履修(人数)制限」ページの取得・申込。

【未検証】実サイトへの実通信は確認できていない。ユーザー提供の実HTML
（ログイン画面のボタン部分、履修(人数)制限ページ全体）を元に構成した。

ログイン画面はSeciossAuthHub系。hidden項目 op/back/sessid を保持したまま
ID・パスワードを入力して送信し、SAMLの中継フォーム(SAMLResponse等)を
自動で辿ってKULASISに戻る、という一般的なSSOの流れを想定している。

多要素認証: ログイン画面には最初から「ワンタイムパスワード送信」ボタンが
style="display:none"で埋め込まれている（毎回そう）。これ自体はMFA要求の
証拠にならないため、_looks_like_mfa()はこのボタンが非表示のままかどうかで
判定する。OTP入力欄が実際に表示された場合のみ MfaRequired を投げて止まる。
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; kulasis-waitlist/0.1; personal use)"
MAX_HOPS = 15
SAML_FIELDS = {"SAMLResponse", "SAMLRequest", "RelayState"}


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


def _style_hides(tag) -> bool:
    style = (tag.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


def _looks_like_mfa(html: str) -> bool:
    """OTP入力が実際に必要になった状態かどうか。誤検知に注意（本文注記参照）。"""
    soup = BeautifulSoup(html, "lxml")
    otp_button = soup.find(id="otp_send_button")
    if otp_button is not None and not _style_hides(otp_button):
        return True
    for inp in soup.find_all("input", id=re.compile(r"otp", re.I)):
        if inp.get("id") in ("otp_send_button", "otp_resend_button"):
            continue
        if not _style_hides(inp):
            return True
    return False


def _pick_form(soup: BeautifulSoup):
    """自動で送信してよいフォームだけを返す: パスワード入力欄付き、またはSAML受け渡し用。"""
    for form in soup.find_all("form"):
        if form.find("input", {"type": "password"}):
            return form
        names = {i.get("name") for i in form.find_all("input")}
        if names & SAML_FIELDS:
            return form
    return None


def _form_data(form) -> dict[str, str]:
    data: dict[str, str] = {}
    seen_submit = False
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        t = (inp.get("type") or "text").lower()
        if t in ("button", "image", "reset"):
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
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        r = self.s.request(method, url, timeout=self.timeout, **kw)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return r

    def login(self, user: str, password: str) -> None:
        r = self._req("GET", self.cfg["login"]["start_url"])
        submitted_credentials = False
        for _ in range(MAX_HOPS):
            if _looks_like_mfa(r.text):
                raise MfaRequired("多要素認証(ワンタイムパスワード)を求められました。自動ログインできません")
            form = _pick_form(BeautifulSoup(r.text, "lxml"))
            if form is None:
                return  # 認証フォームもSAML受け渡しも無い = ログイン後のページに到達
            data = _form_data(form)
            pw = form.find("input", {"type": "password"})
            if pw is not None:
                if submitted_credentials:
                    raise LoginError("ID/パスワードが受け付けられませんでした(認証画面に戻された)")
                user_field = next(
                    (
                        i.get("name")
                        for i in form.find_all("input")
                        if i.get("name") and (i.get("type") or "text").lower() in ("text", "email")
                    ),
                    None,
                )
                if not user_field or not pw.get("name"):
                    raise LoginError("ログインフォームの入力欄名を特定できませんでした")
                data[user_field] = user
                data[pw["name"]] = password
                submitted_credentials = True
            method = (form.get("method") or "post").upper()
            action = urljoin(r.url, form.get("action") or r.url)
            kw = {"params": data} if method == "GET" else {"data": data}
            r = self._req(method, action, **kw)
        raise LoginError(f"リダイレクト/フォーム送信が{MAX_HOPS}回を超えました")

    def fetch_entrylimit_page(self) -> str:
        """履修(人数)制限ページ（全科目一覧）を取得する。"""
        url = self.cfg["pages"]["entrylimit"]
        r = self._req("GET", url)
        if _has_password_field(r.text):
            raise FetchError("セッション切れ: ログイン画面に戻されました")
        return r.text

    def apply(self, lecture_no: str) -> str:
        """entrylimitLectureNoへの申込を送信する。

        【未検証】確認画面が挟まる場合を想定し、同じ形の隠しフィールド
        (entrylimitLectureNo)を持つフォームが続く限り最大3回まで送信を続ける。
        実際に1クリックで確定するのか、確認ステップが要るのかはサイトの
        実挙動を見ないと分からない。必ず --dry-run-apply などで挙動を
        確認してから本番実行すること。
        """
        base = self.cfg["pages"]["entrylimit"]
        r = self._req("POST", urljoin(base, "regist_check"), data={"entrylimitLectureNo": lecture_no})
        for _ in range(3):
            soup = BeautifulSoup(r.text, "lxml")
            form = None
            for f in soup.find_all("form"):
                if f.find("input", {"name": "entrylimitLectureNo"}):
                    form = f
                    break
            if form is None:
                break
            action = urljoin(r.url, form.get("action") or r.url)
            r = self._req("POST", action, data=_form_data(form))
        if _has_password_field(r.text):
            raise ApplyError("セッション切れ: ログイン画面に戻されました")
        return r.text
