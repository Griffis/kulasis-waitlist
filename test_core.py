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


def _apply_client(posts, timeslots, cached=None):
    """posts: candidate_add への応答/例外の列。timeslots: 通信で取得する時間割ページHTMLの列。
    cached: 事前取得済みの時間割ページHTML（申込前の確認に使う。None なら事前確認なし）。"""
    client = KulasisClient({})
    post_calls, ts_iter = [], iter(timeslots)
    client._timeslot_html = lambda: next(ts_iter)
    if cached is not None:
        client._timeslot_cache = (kc.time.monotonic(), cached)

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


def test_apply_skips_when_cached_timetable_already_has_course():
    client, posts = _apply_client([_added()], [], cached=REGISTERED)
    assert client.apply("63816") is False
    assert posts == []


def test_apply_does_no_extra_request_before_posting_without_cache():
    # 申込前の確認のための通信はしない（混雑時にページ遷移を増やさない）。POSTの応答で成否を確認する
    client, posts = _apply_client([_added()], [])  # timeslots が空: 通信で取得しようとすると StopIteration
    assert client.apply("63816") is True
    assert len(posts) == 1


def test_apply_checks_timetable_before_resending_after_502():
    err = requests.HTTPError("502", response=FakeResp(502))
    client, posts = _apply_client([err], [REGISTERED])  # 502 → 反映を確認 → 登録済み
    assert client.apply("63816") is True
    assert len(posts) == 1


def test_apply_resends_after_502_when_not_registered():
    err = requests.HTTPError("502", response=FakeResp(502))
    client, posts = _apply_client([err, _added()], [""])  # 502 → 確認=未登録 → 再送で成功
    assert client.apply("63816") is True
    assert len(posts) == 2


def test_apply_raises_without_resend_when_not_in_timetable_after_success_response():
    # 送信は成功(302→timeslot_list)したが科目が載らない: 反映待ちの確認し直し(2回)のあと失敗。再送はしない
    client, posts = _apply_client([_added("")], ["", ""])
    with pytest.raises(ApplyError):
        client.apply("63816")
    assert len(posts) == 1


def test_apply_waits_for_delayed_reflection():
    client, posts = _apply_client([_added("")], ["", REGISTERED])  # 1回目の確認では未反映、再確認で反映
    assert client.apply("63816") is True
    assert len(posts) == 1


class _FakeLoginClient:
    plan: list = []
    made = 0
    otp_sent = False

    def __init__(self, cfg, profile=None):
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

# ---------------------------------------------------------------------------
# 何が出ても対応するための分類・待ち時間・ページ内容の検証
# ---------------------------------------------------------------------------
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone


def _http_err(status, headers=None):
    resp = FakeResp(status)
    resp.headers = headers or {}
    return requests.HTTPError(str(status), response=resp)


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504, 507, 520, 524, 599])
def test_is_transient_retryable_statuses(status):
    assert kc.is_transient(_http_err(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 501, 505, 511])
def test_is_transient_non_retryable_statuses(status):
    assert kc.is_transient(_http_err(status)) is False


def test_is_transient_exceptions():
    for exc in (
        requests.ConnectionError(), requests.Timeout(), requests.exceptions.ConnectTimeout(),
        requests.exceptions.ReadTimeout(), requests.exceptions.SSLError(), requests.exceptions.ProxyError(),
        requests.exceptions.ChunkedEncodingError(), requests.exceptions.ContentDecodingError(),
        kc.UnexpectedPage("maintenance"), kc.UnexpectedLoginStep("unknown step"),
    ):
        assert kc.is_transient(exc) is True, type(exc).__name__
    for exc in (
        requests.exceptions.InvalidURL(), requests.exceptions.TooManyRedirects(), requests.exceptions.MissingSchema(),
        kc.LoginError("bad password"), kc.MfaRequired("no secret"), kc.ApplyError("x"), ValueError("x"),
    ):
        assert kc.is_transient(exc) is False, type(exc).__name__


def test_retry_wait_backs_off_and_stays_bounded():
    # 混雑時に連打しないよう、1秒から倍々に広げる（根拠: 履修登録ページは「更新を押さず待つ」運用）
    waits = [kc._retry_wait(n) for n in (1, 2, 3, 4)]
    assert 0.8 <= waits[0] <= 1.2 and 1.6 <= waits[1] <= 2.4 and 3.2 <= waits[2] <= 4.8 and 6.4 <= waits[3] <= 9.6
    assert sum(waits) < 20


def test_apply_waits_are_shorter_than_get_waits():
    first = kc._retry_wait(1, None, kc.APPLY_RETRY_WAITS)
    assert 0.4 <= first <= 0.6


def test_retry_wait_honors_retry_after_with_cap():
    assert kc._retry_wait(1, _http_err(429, {"Retry-After": "3"})) == 3.0
    assert kc._retry_wait(1, _http_err(503, {"Retry-After": "9999"})) == kc.RETRY_AFTER_CAP_SEC
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5), usegmt=True)
    assert 3.0 <= kc._retry_wait(1, _http_err(429, {"Retry-After": when})) <= 5.0
    assert kc._retry_wait(1, _http_err(429)) >= kc.RATE_LIMIT_MIN_WAIT * 0.8  # ヘッダ無しでも連打しない
    assert kc._retry_wait(1, _http_err(429, {"Retry-After": "abc"})) >= kc.RATE_LIMIT_MIN_WAIT * 0.8


def test_req_resends_get_on_chunked_encoding_error_and_429():
    # _client_with は例外をそのまま送出し、レスポンスは返す。429はFakeRespで再現する
    client, calls = _client_with([requests.exceptions.ChunkedEncodingError(), FakeResp(429), FakeResp(200, "ok")])
    assert client._req("GET", "https://example.test/x").text == "ok"
    assert len(calls) == 3


SEARCH_OK = "<html>検索結果は全部で<b>0</b>件です。</html>"


def _search_client(pages):
    client = KulasisClient({})
    client._warmed = True
    calls = []

    def fake_req(method, url, **kw):
        calls.append(url)
        return FakeResp(200, pages[min(len(calls) - 1, len(pages) - 1)])

    client._req = fake_req
    return client, calls


def test_fetch_retries_when_http200_but_maintenance_page():
    client, calls = _search_client(["<html>ただいまメンテナンス中です</html>"] * 2 + [SEARCH_OK])
    pages = client.fetch_lecture_search("人文地理学")
    assert len(calls) == 3 and pages == [SEARCH_OK]


def test_fetch_raises_unexpected_page_after_5_attempts():
    client, calls = _search_client(["<html>エラー</html>"])
    with pytest.raises(kc.UnexpectedPage):
        client.fetch_lecture_search("人文地理学")
    assert len(calls) == 5


def test_login_with_retry_retries_unexpected_login_step(monkeypatch):
    _fake_login(monkeypatch, [kc.UnexpectedLoginStep("maintenance"), None])
    _, n_err, errors = main_mod.login_with_retry({}, "u", "p", "s")
    assert n_err == 1 and errors == ["UnexpectedLoginStep"]


# ---------------------------------------------------------------------------
# 混雑対策: 読み取り待ちの長さ・実行上限・時間割ページの使い回し・準備の省略
# ---------------------------------------------------------------------------
def test_read_timeout_is_long_only_for_timeslot_pages():
    client = KulasisClient({}, profile=PROFILES["watch"])
    assert client._read_timeout("https://www.k.kyoto-u.ac.jp/student/la/timeslot/lecture_search?x=1") == 120.0
    assert client._read_timeout("https://www.k.kyoto-u.ac.jp/student/la/timeslot/candidate_add") == 120.0
    assert client._read_timeout("https://authidp1.iimc.kyoto-u.ac.jp/idp/profile/SAML2/Redirect/SSO") == kc.READ_TIMEOUT_SEC
    rush = KulasisClient({}, profile=PROFILES["rush"])
    assert rush._read_timeout("https://www.k.kyoto-u.ac.jp/student/la/timeslot/candidate_add") == 240.0


def test_read_timeout_never_exceeds_run_deadline(monkeypatch):
    client = KulasisClient({})
    monkeypatch.setattr(kc, "_T0", kc.time.monotonic() - (kc.RUN_DEADLINE_SEC - 30))
    assert client._read_timeout("https://x.test/student/la/timeslot/top") <= 30.5
    monkeypatch.setattr(kc, "_T0", kc.time.monotonic() - (kc.RUN_DEADLINE_SEC + 100))
    assert client._read_timeout("https://x.test/student/la/timeslot/top") == 5.0  # 下限


def test_timeslot_cache_is_filled_by_fetch_and_used_without_traffic():
    client = KulasisClient({})
    calls = []
    client._req = lambda method, url, **kw: (calls.append(url), FakeResp(200, REGISTERED))[1]
    assert client._cached_timeslot_html(60) is None       # 取得前は無い（通信もしない）
    assert client._timeslot_html() == REGISTERED          # 取得してキャッシュ
    assert client._cached_timeslot_html(60) == REGISTERED  # キャッシュを返す（通信しない）
    assert client.apply("63816") is False                  # 申込前の確認もキャッシュで済み、通信は増えない
    assert len(calls) == 1
    client._timeslot_cache = (kc.time.monotonic() - 500, REGISTERED)
    assert client._cached_timeslot_html(120) is None       # 古いキャッシュは使わない


def test_apply_invalidates_cache_after_post():
    client = KulasisClient({})
    client._timeslot_cache = (kc.time.monotonic(), "")     # 未登録のキャッシュ
    seen = {}

    def fake_req(method, url, **kw):
        if method == "POST":
            seen["cache_after_post_start"] = client._timeslot_cache
            return _added()
        return FakeResp(200, REGISTERED)

    client._req = fake_req
    assert client.apply("63816") is True
    assert seen["cache_after_post_start"] is None


def test_warmup_follows_profile():
    rush = KulasisClient({}, profile=PROFILES["rush"])
    rush._req = lambda *a, **k: (_ for _ in ()).throw(AssertionError("rushでは事前取得しないはず"))
    rush._warmup()
    watch = KulasisClient({}, profile=PROFILES["watch"])
    calls = []
    watch._req = lambda method, url, **kw: (calls.append(url.rsplit("/", 1)[-1]), FakeResp(200, REGISTERED))[1]
    watch._warmup()
    assert calls == ["top", "timeslot_list"]


def test_login_retry_waits_grow(monkeypatch):
    slept = []
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: slept.append(s))
    _fake_login(monkeypatch, [_e(502), _e(503), _e(502), None])
    main_mod.login_with_retry({}, "u", "p", "s")
    assert slept == [2.0, 4.0, 8.0]


# ---------------------------------------------------------------------------
# 動作モード（watch / rush）の切り替え
# ---------------------------------------------------------------------------
from src.config import PROFILES, resolve_profile


def test_resolve_profile_priority():
    cfg = {"mode": "rush"}
    assert resolve_profile({}).name == "watch"                          # 既定
    assert resolve_profile(cfg).name == "rush"                          # config.yml
    assert resolve_profile(cfg, env_mode="watch").name == "watch"       # 環境変数 > config.yml
    assert resolve_profile(cfg, cli_mode="watch", env_mode="rush").name == "watch"  # --mode が最優先
    assert resolve_profile(cfg, env_mode="").name == "rush"             # 環境変数が空なら config.yml（workflowの入力が空欄のとき）
    assert resolve_profile({}, cli_mode=" RUSH ").name == "rush"        # 前後の空白・大文字小文字を吸収


def test_profiles_differ_as_intended():
    watch, rush = PROFILES["watch"], PROFILES["rush"]
    assert watch.warmup is True and rush.warmup is False                # rush はページ遷移を減らす
    assert rush.timeslot_read_timeout_sec > watch.timeslot_read_timeout_sec
    assert rush.course_gap_sec < watch.course_gap_sec


def test_resolve_profile_overrides_and_validation():
    p = resolve_profile({"mode": "rush", "modes": {"rush": {"warmup": True, "timeslot_read_timeout_sec": "300"}}})
    assert p.warmup is True and p.timeslot_read_timeout_sec == 300.0 and p.course_gap_sec == 0.0
    with pytest.raises(ValueError):
        resolve_profile({"mode": "turbo"})
    with pytest.raises(ValueError):
        resolve_profile({"mode": "rush", "modes": {"rush": {"warmup_pages": 3}}})


# ---------------------------------------------------------------------------
# 申込の再送: 応答に時間がかかった失敗は、確認を重ねてから再送する / 重複検証ツール
# ---------------------------------------------------------------------------
def test_apply_settles_longer_after_slow_failure(monkeypatch):
    slept = []
    monkeypatch.setattr(kc.time, "sleep", lambda sec: slept.append(sec))
    err = requests.HTTPError("504", response=FakeResp(504))
    client = KulasisClient({})
    ticks = iter([0.0, 30.0, 30.0, 31.0, 31.0, 31.0, 31.0, 31.0, 31.0, 31.0, 31.0])  # 1回目の送信が30秒かかって失敗
    monkeypatch.setattr(kc.time, "monotonic", lambda: next(ticks, 31.0))
    ts = iter(["", "", REGISTERED])  # 1回目の確認=未反映, 2回目=未反映, 3回目=反映済み
    client._timeslot_html = lambda: next(ts)
    posts = []

    def fake_req(method, url, **kw):
        posts.append(url)
        raise err

    client._req = fake_req
    assert client.apply("63816") is True
    assert len(posts) == 1                      # 元のリクエストが処理中だった想定: 再送せずに反映を確認できた
    assert kc.APPLY_SETTLE_WAITS[0] in slept and kc.APPLY_SETTLE_WAITS[1] in slept


def test_debug_double_apply_reports_duplicate(capsys):
    client = KulasisClient({})
    pages = iter([
        FakeResp(200, REGISTERED, url="https://x.test/student/la/timeslot/timeslot_list"),
        FakeResp(200, REGISTERED + REGISTERED + "<p>既に追加されています</p>", url="https://x.test/student/la/timeslot/timeslot_list"),
    ])
    client._req = lambda method, url, **kw: next(pages)
    client.debug_double_apply("63816")
    out = capsys.readouterr().out
    assert "出現数=1" in out and "出現数=2" in out
    assert "重複して登録された" in out and "既に追加されています" in out