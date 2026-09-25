from __future__ import annotations

import os
import re
from urllib.parse import urljoin

import pyotp
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
    for form in soup.find_all("form"):
        if form.find("input", {"type": "password"}):
            return form
        names = {i.get("name") for i in form.find_all("input")}
        if names & SAML_FIELDS:
            return form
        # OTP入力フォームの検出
        if form.find("input", {"id": re.compile(r"otp", re.I)}) or form.find("input", {"name": re.compile(r"otp", re.I)}):
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

    def login(self, user: str, password: str, totp_secret: str | None = None) -> None:
        r = self._req("GET", self.cfg["login"]["start_url"])
        submitted_credentials = False
        submitted_otp = False

        for _ in range(MAX_HOPS):
            soup = BeautifulSoup(r.text, "lxml")
            
            # OTP入力要求の判定と送信処理
            if _looks_like_mfa(r.text):
                if not totp_secret:
                    raise MfaRequired("TOTP_SECRETが設定されていないため、2段階認証を通過できません")
                if submitted_otp:
                    raise LoginError("ワンタイムパスワードが拒否されました")

                otp_field = soup.find("input", {"id": re.compile(r"otp", re.I)}) or soup.find("input", {"name": re.compile(r"otp", re.I)})
                if not otp_field or not otp_field.get("name"):
                    raise LoginError("OTP入力フィールド名を特定できませんでした")

                form = otp_field.find_parent("form") or _pick_form(soup)
                data = _form_data(form)
                
                # pyotpで6桁のコードを生成して設定
                otp_code = pyotp.TOTP(totp_secret).now()
                data[otp_field["name"]] = otp_code
                submitted_otp = True

                action = urljoin(r.url, form.get("action") or r.url)
                method = (form.get("method") or "post").upper()
                kw = {"params": data} if method == "GET" else {"data": data}
                r = self._req(method, action, **kw)
                continue

            form = _pick_form(soup)
            if form is None:
                return  # ログイン完了

            data = _form_data(form)
            pw = form.find("input", {"type": "password"})
            if pw is not None:
                if submitted_credentials:
                    raise LoginError("ID/パスワードが受け付けられませんでした")
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
        url = self.cfg["pages"]["entrylimit"]
        r = self._req("GET", url)
        if _has_password_field(r.text):
            raise FetchError("セッション切れ: ログイン画面に戻されました")
        return r.text

    def apply(self, lecture_no: str) -> str:
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