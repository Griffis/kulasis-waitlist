"""パーサ・照合・状態遷移のテスト。HTMLは実サイトのものではなく、lecture_searchの列構成を模した合成データ。"""
import requests
from types import SimpleNamespace
from src.main import _is_transient
from src.config import Course, norm
from src.main import build_message, find_row, format_course_status, status_of
from src.parser import parse_lecture_search
from src.state import transition_kind

OPEN_OP = """
<form action="candidate_add" method="post" class="candidate-add-form fcfs_candidate_add_form">
  <input type="hidden" name="lectureNo" value="{lno}" />
  <input type="image" src="/img/button_mini_add.gif" alt="追加" />
</form>
"""
FULL_OP = '<img src="/img/button_mini_add_disable.gif" >'

ROW_TMPL = """
<tr class="odd_normal">
  <td>{code}</td>
  <td>{name} </td>
  <td>{teacher}</td>
  <td>{day_period}</td>
  <td>共北21</td>
  <td>後期</td>
  <td>人社群</td>
  <td>A群</td>
  <td>{seats}</td>
  <td>
    <table><tr>
      <td><a href="candidate_add_detail?dbName=la&lectureNo={lno}"><img alt="詳細"></a></td>
      <td>{op}</td>
    </tr><tr><td colspan="2">科目名変更等一覧</td></tr></table>
  </td>
</tr>
"""

HEADER = """
<table class="standard_list"><tbody>
<tr class="th_normal">
  <td>講義コード</td><td>科目名</td><td>担当教員</td><td>曜時限</td><td>教室名</td>
  <td>開講期</td><td>群</td><td>旧群</td><td>申込者数/定員</td><td></td>
</tr>
"""


def _row(code, name, teacher, day_period, seats, lno, has_room):
    op = OPEN_OP.format(lno=lno) if has_room else FULL_OP
    return ROW_TMPL.format(code=code, name=name, teacher=teacher,
                           day_period=day_period, seats=seats, lno=lno, op=op)


SAMPLE = HEADER + "".join([
    _row("H001001", "人文地理学", "甲", "月2", "29/30", "1001", True),
    _row("H002001", "宗教学Ⅱ", "乙", "火2", "40/40", "1002", False),
    _row("H003001", "Programming Practice (Python) -E2", "丙", "水5", "10/60", "1003", True),
]) + "</tbody></table>"


def test_norm_absorbs_roman_numerals_and_width():
    assert norm("宗教学Ⅱ") == norm("宗教学II") == "宗教学II"


def test_parse_lecture_search():
    rows = parse_lecture_search(SAMPLE)
    assert len(rows) == 3  # ヘッダ行・入れ子テーブル内の行は数えない
    assert rows[0].applicants == 29 and rows[0].capacity == 30
    assert rows[0].lecture_no == "1001"
    assert rows[0].lecture_code == "H001001"
    assert rows[1].lecture_no is None  # 満席行には追加フォームが無い
    assert rows[0].already_applied is False


def test_find_row_and_status():
    rows = parse_lecture_search(SAMPLE)
    c1 = Course("人文地理学", "月", 2)
    c2 = Course("宗教学II", "火", 2)
    c3 = Course("言語学II", "木", 3)
    c4 = Course("Programming Practice (Python) -E2", "水", 5)
    assert status_of(find_row(c1, rows)) == "available"
    assert status_of(find_row(c2, rows)) == "full"
    assert status_of(find_row(c3, rows)) == "not_found"
    assert status_of(find_row(c4, rows)) == "available"


def test_transitions():
    assert transition_kind(None, "available") == "available"
    assert transition_kind("full", "available") == "available"
    assert transition_kind("available", "available") is None
    assert transition_kind(None, "full") is None
    assert transition_kind("available", "full") == "closed"
    assert transition_kind(None, "not_found") == "warn"


def test_build_message_mentions_auto_apply():
    rows = parse_lecture_search(SAMPLE)
    c = Course("人文地理学", "月", 2)
    row = find_row(c, rows)
    msg = build_message("available", c, row, applied=True)
    assert "自動で申込" in msg


def test_format_course_status_is_four_lines_and_labels_full():
    rows = parse_lecture_search(SAMPLE)
    c = Course("宗教学II", "火", 2)
    message = format_course_status(
        c, find_row(c, rows), "not_found", "full",
        auto_apply=True, kulasis_applied=False, recorded_applied=False, fail_count=0,
    )
    assert message.splitlines() == [
        "❌ 満席（席が埋まっています）  火2 宗教学II",
        "     席数: 40/40人（残り0席）",
        "     前回→今回: 見つからず → 満席",
        "     自動申込: ON / KULASIS上の申込: なし / 申込済みの記録: なし / 申込失敗の連続: 0回",
    ]

def test_is_transient():
    assert _is_transient(requests.ConnectionError()) is True
    assert _is_transient(requests.Timeout()) is True
    assert _is_transient(requests.HTTPError(response=SimpleNamespace(status_code=502))) is True
    assert _is_transient(requests.HTTPError(response=SimpleNamespace(status_code=401))) is False
    assert _is_transient(ValueError("x")) is False


# ---------------------------------------------------------------------------
# 再試行（第1層: 1リクエストの再送 / 申込の再送 / 第2層: ログインのやり直し）
# ---------------------------------------------------------------------------
import pytest

import src.kulasis_client as kc
import src.main as main_mod
from src.kulasis_client import ApplyError, KulasisClient, KulasisError


class FakeResp:
    def __init__(self, status=200, text="", url="https://example.test/x"):
        self.status_code, self.text, self.url = status, text, url
        self.encoding, self.history, self.headers = "utf-8", [], {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(kc.time, "sleep", lambda s: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(kc, "budget_left", lambda: True)
    monkeypatch.setattr(main_mod, "budget_left", lambda: True)


def _client_with(responses):
    client = KulasisClient({})
    calls = []

    def fake_request(method, url, **kw):
        calls.append((method, url))
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    client.session.request = fake_request
    return client, calls


def test_req_resends_after_502_then_succeeds():
    client, calls = _client_with([FakeResp(502), FakeResp(502), FakeResp(200, "ok")])
    assert client._req("GET", "https://example.test/x").text == "ok"
    assert len(calls) == 3


def test_req_gives_up_after_5_attempts():
    client, calls = _client_with([FakeResp(502)])
    with pytest.raises(requests.HTTPError):
        client._req("GET", "https://example.test/x")
    assert len(calls) == 5


def test_req_does_not_resend_non_transient_or_retry_false():
    client, calls = _client_with([FakeResp(401)])
    with pytest.raises(requests.HTTPError):
        client._req("GET", "https://example.test/x")
    assert len(calls) == 1
    client, calls = _client_with([FakeResp(502)])
    with pytest.raises(requests.HTTPError):
        client._req("GET", "https://example.test/x", retry=False)
    assert len(calls) == 1


def test_req_never_resends_post():
    # 実測: 認証画面のPOSTが502 → 再送すると500。POSTは再送せず、ログインのやり直しに任せる。
    client, calls = _client_with([FakeResp(502), FakeResp(500)])
    with pytest.raises(requests.HTTPError):
        client._req("POST", "https://example.test/idp/profile/SAML2/Redirect/SSO")
    assert len(calls) == 1


def test_req_resends_get_on_500():
    client, calls = _client_with([FakeResp(500), FakeResp(200, "ok")])
    assert client._req("GET", "https://example.test/x").text == "ok"
    assert len(calls) == 2


REGISTERED = '<a href="/student/la/support/top?no=63816&from=x">全共:Biologi..</a>'


def _apply_client(posts, timeslots):
    """posts: candidate_add への応答/例外の列。timeslots: 時間割ページHTMLの列。"""
    client = KulasisClient({})
    post_calls, ts_iter = [], iter(timeslots)
    client._timeslot_html = lambda: next(ts_iter)

    def fake_req(method, url, **kw):
        post_calls.append(url)
        item = posts[min(len(post_calls) - 1, len(posts) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    client._req = fake_req
    return client, post_calls


def _added(text=REGISTERED):
    return FakeResp(200, text, url="https://example.test/student/la/timeslot/timeslot_list?server=callisto")


def test_apply_skips_when_already_in_timetable():
    client, posts = _apply_client([_added()], [REGISTERED])
    assert client.apply("63816") is False
    assert posts == []


def test_apply_checks_timetable_before_resending_after_502():
    err = requests.HTTPError("502", response=FakeResp(502))
    client, posts = _apply_client([err], ["", REGISTERED])  # 事前確認=未登録 → 502 → 再確認=登録済み
    assert client.apply("63816") is True
    assert len(posts) == 1


def test_apply_resends_after_502_when_not_registered():
    err = requests.HTTPError("502", response=FakeResp(502))
    client, posts = _apply_client([err, _added()], ["", ""])  # 事前=未登録 / 502後の確認=未登録 → 再送で成功
    assert client.apply("63816") is True
    assert len(posts) == 2


def test_apply_raises_without_resend_when_not_in_timetable_after_success_response():
    client, posts = _apply_client([_added("")], [""])
    with pytest.raises(ApplyError):
        client.apply("63816")
    assert len(posts) == 1


class _FakeLoginClient:
    plan: list = []
    made = 0
    otp_sent = False

    def __init__(self, cfg):
        type(self).made += 1

    def login(self, user, password, totp_secret=None):
        item = type(self).plan[type(self).made - 1]
        if item is not None:
            raise item


def _fake_login(monkeypatch, plan):
    _FakeLoginClient.plan, _FakeLoginClient.made = plan, 0
    monkeypatch.setattr(main_mod, "KulasisClient", _FakeLoginClient)


def _e(status):
    return requests.HTTPError(str(status), response=FakeResp(status))


def test_login_with_retry_recovers_after_502(monkeypatch):
    _fake_login(monkeypatch, [_e(502), _e(500), None])
    _, n_err, errors = main_mod.login_with_retry({}, "u", "p", "s")
    assert n_err == 2 and errors == ["HTTP502", "HTTP500"]


def test_login_with_retry_exhausted_and_non_transient(monkeypatch):
    _fake_login(monkeypatch, [_e(502)] * 5)
    with pytest.raises(main_mod.LoginRetryExhausted):
        main_mod.login_with_retry({}, "u", "p", "s")
    assert _FakeLoginClient.made == 5
    _fake_login(monkeypatch, [kc.LoginError("bad password")])
    with pytest.raises(kc.LoginError):
        main_mod.login_with_retry({}, "u", "p", "s")
    assert _FakeLoginClient.made == 1