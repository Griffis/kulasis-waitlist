"""動作モード・lecture_no・経過秒ログ・rush の直接申込のテスト。

通信はしない。KulasisClient は偽物に差し替える。パーサの中身には依存しない（parse_lecture_search は差し替える）。
"""
import re

import pytest
import requests

from src import kulasis_client as kc
from src import main as m
from src.config import PROFILES, Course, load_courses, resolve_profile
from src.kulasis_client import ApplyError, KulasisClient
from src.logutil import log, log_start, timed


# ---------------------------------------------------------------- Profile
def test_watch_is_fast_and_rush_is_patient():
    w, r = PROFILES["watch"], PROFILES["rush"]
    assert w.warmup is False and r.warmup is False
    assert w.timeslot_read_timeout_sec < r.timeslot_read_timeout_sec
    assert w.run_deadline_sec < r.run_deadline_sec
    assert w.req_max_attempts < r.req_max_attempts
    assert w.course_gap_sec == 0 and r.course_gap_sec == 0
    assert w.direct_apply is False and r.direct_apply is True


def test_profile_override_converts_types():
    p = resolve_profile({"modes": {"watch": {"retry_waits": [0.1, 1], "req_max_attempts": "2", "warmup": 1}}}, "watch")
    assert p.retry_waits == (0.1, 1.0) and p.req_max_attempts == 2 and p.warmup is True


@pytest.mark.parametrize("bad", [{"retry_waits": []}, {"req_max_attempts": 0}, {"nope": 1}])
def test_profile_override_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        resolve_profile({"modes": {"rush": bad}}, "rush")


# ---------------------------------------------------------------- courses.yml
def _write(tmp_path, body):
    p = tmp_path / "courses.yml"
    p.write_text(body, encoding="utf-8")
    return p


def test_lecture_no_keeps_leading_zero_and_period_is_int(tmp_path):
    p = _write(tmp_path, "courses:\n  - name: A\n    day: 月\n    period: 2\n    lecture_no: 064073\n")
    (c,) = load_courses(p)
    assert c.lecture_no == "064073" and c.period == 2


def test_lecture_no_optional_and_validated(tmp_path):
    (c,) = load_courses(_write(tmp_path, "courses:\n  - name: A\n    day: 月\n    period: 2\n"))
    assert c.lecture_no == ""
    with pytest.raises(ValueError):
        load_courses(_write(tmp_path, "courses:\n  - name: A\n    day: 月\n    period: 2\n    lecture_no: 12ab\n"))


def test_period_range_still_validated(tmp_path):
    with pytest.raises(ValueError):
        load_courses(_write(tmp_path, "courses:\n  - name: A\n    day: 月\n    period: 9\n"))


# ---------------------------------------------------------------- ログ
LINE = re.compile(r"^\[\s*\d+\.\d{2}s\] \[(?P<tag>[^\]]+)\] (?P<msg>.*)$")


def test_log_prefix_has_elapsed_seconds(capsys):
    log("準備", "テスト")
    m_ = LINE.match(capsys.readouterr().out.strip())
    assert m_ and m_["tag"] == "準備" and m_["msg"] == "テスト"


def test_log_start_shows_jst_and_utc(capsys):
    log_start()
    out = capsys.readouterr().out
    assert "JST" in out and "UTC" in out and "[開始]" in out


def test_timed_logs_success_and_failure(capsys):
    with timed("検索", "処理A"):
        pass
    with pytest.raises(RuntimeError):
        with timed("検索", "処理B"):
            raise RuntimeError("x")
    out = capsys.readouterr().out
    assert "⏱ 処理A" in out and "❌ 処理B" in out


# ---------------------------------------------------------------- クライアント
class _Boom(Exception):
    pass


def _count_attempts(client, monkeypatch):
    calls = []
    monkeypatch.setattr(kc.time, "sleep", lambda s: None)

    def fake_request(method, url, **kw):
        calls.append(url)
        raise requests.ConnectionError("down")

    client.session.request = fake_request
    with pytest.raises(requests.ConnectionError):
        client._req("GET", "https://example.invalid/x")
    return len(calls)


def test_client_get_retries_follow_profile(monkeypatch):
    assert _count_attempts(KulasisClient({}, profile=PROFILES["watch"]), monkeypatch) == PROFILES["watch"].req_max_attempts
    assert _count_attempts(KulasisClient({}, profile=PROFILES["rush"]), monkeypatch) == PROFILES["rush"].req_max_attempts


def test_client_without_profile_keeps_legacy_constants(monkeypatch):
    assert _count_attempts(KulasisClient({}), monkeypatch) == kc.REQ_MAX_ATTEMPTS


def test_set_run_limits_changes_budget(monkeypatch):
    old = (kc.RETRY_BUDGET_SEC, kc.RUN_DEADLINE_SEC)
    try:
        kc.set_run_limits(0, 0)
        assert kc.budget_left() is False
        kc.set_run_limits(10_000, 10_000)
        assert kc.budget_left() is True
    finally:
        kc.set_run_limits(*old)


def test_warmup_logs_skip_when_off(capsys):
    KulasisClient({}, profile=PROFILES["watch"])._warmup()
    assert "[準備]" in capsys.readouterr().out


def test_warmup_logs_success_when_on(capsys, monkeypatch):
    from dataclasses import replace

    c = KulasisClient({}, profile=replace(PROFILES["watch"], warmup=True))
    monkeypatch.setattr(c, "_req", lambda *a, **k: None)
    monkeypatch.setattr(c, "_timeslot_html", lambda: "")
    c._warmup()
    out = capsys.readouterr().out
    assert "⏱ 事前取得 top" in out and "⏱ 事前取得 timeslot_list" in out


# ---------------------------------------------------------------- main: 直接申込
class FakeClient:
    def __init__(self, fail_numbers=()):
        self.applied, self.searched, self.fail = [], [], set(fail_numbers)

    def apply(self, lecture_no):
        if lecture_no in self.fail:
            raise ApplyError("追加後の時間割に科目が見当たらない")
        self.applied.append(lecture_no)
        return True

    def fetch_lecture_search(self, title):
        self.searched.append(title)
        return ["<html></html>"]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("KULASIS_USER", "u")
    monkeypatch.setenv("KULASIS_PASSWORD", "p")
    monkeypatch.delenv("KULASIS_MODE", raising=False)
    monkeypatch.delenv("TOTP_SECRET", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    sent, saved = [], []
    holder = {"client": FakeClient(), "state": {"courses": {}, "last_error": None}}
    monkeypatch.setattr(m, "send_discord", lambda url, msg: sent.append(msg))
    monkeypatch.setattr(m, "load_state", lambda: holder["state"])
    monkeypatch.setattr(m, "save_state", lambda st: saved.append(st))
    monkeypatch.setattr(m, "parse_lecture_search", lambda html: [])
    monkeypatch.setattr(m, "login_with_retry", lambda *a, **k: (holder["client"], 0, []))
    monkeypatch.setattr(m, "load_config", lambda: {"apply": {"auto_apply": True, "fail_notify_threshold": 1}})
    holder.update(sent=sent, saved=saved)
    return holder


def _courses(monkeypatch, *cs):
    monkeypatch.setattr(m, "load_courses", lambda: list(cs))


A = Course("科目A", "月", 1, lecture_no="064073")
B = Course("科目B", "火", 2, lecture_no="1002")
C = Course("科目C", "水", 3)  # lecture_no なし


def test_rush_sends_directly_in_order_and_skips_search(env, monkeypatch):
    _courses(monkeypatch, A, B)
    assert m.main(["--mode", "rush"]) == 0
    assert env["client"].applied == ["064073", "1002"]  # courses.yml の順
    assert env["client"].searched == []                  # 全科目が直接申込 → 検索なし
    st = env["saved"][-1]["courses"]
    assert st[A.key]["applied"] is True and st[B.key]["status"] == "available"
    assert sum("直接申込を送信した" in s for s in env["sent"]) == 2


def test_rush_mixed_searches_only_courses_without_lecture_no(env, monkeypatch):
    _courses(monkeypatch, A, C)
    m.main(["--mode", "rush"])
    assert env["client"].applied == ["064073"]
    assert env["client"].searched == ["科目C"]


def test_rush_direct_failure_falls_back_to_search_and_counts(env, monkeypatch):
    env["client"] = FakeClient(fail_numbers={"1002"})
    _courses(monkeypatch, A, B)
    m.main(["--mode", "rush"])
    assert env["client"].applied == ["064073"]
    assert env["client"].searched == ["科目B"]  # 失敗した科目だけ状態確認のため検索
    st = env["saved"][-1]["courses"]
    assert st[B.key]["apply_fail_count"] >= 1 and "applied" not in st[B.key]
    assert any("連続で失敗" in s for s in env["sent"])  # しきい値1 → 通知


def test_rush_skips_courses_already_recorded_as_applied(env, monkeypatch):
    env["state"]["courses"][A.key] = {"applied": True, "status": "available"}
    _courses(monkeypatch, A, B)
    m.main(["--mode", "rush"])
    assert env["client"].applied == ["1002"]
    assert env["client"].searched == ["科目A"]  # 申込済みの科目は通常の検索で状態だけ見る


def test_watch_ignores_lecture_no_and_searches(env, monkeypatch):
    _courses(monkeypatch, A, B)
    m.main(["--mode", "watch"])
    assert env["client"].applied == [] and env["client"].searched == ["科目A", "科目B"]


def test_default_mode_is_watch(env, monkeypatch, capsys):
    _courses(monkeypatch, A)
    m.main([])
    assert "[モード] watch" in capsys.readouterr().out


def test_dry_run_does_not_apply_or_save(env, monkeypatch, capsys):
    _courses(monkeypatch, A)
    m.main(["--mode", "rush", "--dry-run"])
    out = capsys.readouterr().out
    assert env["client"].applied == [] and env["saved"] == [] and env["sent"] == []
    assert "[dry-run]" in out


def test_auto_apply_off_means_no_direct_apply(env, monkeypatch):
    monkeypatch.setattr(m, "load_config", lambda: {"apply": {"auto_apply": False}})
    _courses(monkeypatch, A)
    m.main(["--mode", "rush"])
    assert env["client"].applied == [] and env["client"].searched == ["科目A"]


def test_notify_failure_does_not_abort_state_save(env, monkeypatch):
    def boom(url, msg):
        raise requests.ConnectionError("discord down")

    monkeypatch.setattr(m, "send_discord", boom)
    _courses(monkeypatch, A)
    assert m.main(["--mode", "rush"]) == 0
    assert env["saved"], "通知に失敗しても state は保存される"


def test_every_log_line_has_elapsed_prefix(env, monkeypatch, capsys):
    _courses(monkeypatch, A, C)
    m.main(["--mode", "rush"])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[")]
    assert lines and all(LINE.match(ln) for ln in lines)