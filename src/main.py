"""エントリポイント: python -m src.main [--dry-run | --test-discord | --offline-html FILE]"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import requests

from .config import Course, load_config, load_courses, norm
from .kulasis_client import ApplyError, KulasisClient, KulasisError
from .notify import send_discord
from .parser import EntryRow, parse_entrylimit
from .state import load_state, save_state, transition_kind

# .env ファイルから環境変数を自動読み込み
load_dotenv()


def normalize_text(text: str) -> str:
    """比較用に正規化する（config.norm() + 小文字化）。config.pyと正規化処理を共通化。"""
    return norm(text).lower() if text else ""


def find_row(course: Course, rows: list[EntryRow]) -> EntryRow | None:
    """設定された科目名と曜時限に一致する行を検索する"""
    target_needle = normalize_text(course.needle)
    target_day = normalize_text(course.day_period)

    for r in rows:
        row_name = normalize_text(r.name)
        row_day = normalize_text(r.day_period)

        # 曜時限が一致し、検索キーワード（科目名）が含まれているか判定
        if row_day == target_day and target_needle in row_name:
            return r
    return None


def status_of(row: EntryRow | None) -> str:
    if row is None:
        return "not_found"
    return "available" if row.has_room else "full"


def _seats(row: EntryRow | None) -> str:
    if row is None or row.capacity is None:
        return "定員情報なし"
    return f"{row.applicants}/{row.capacity}"


def build_message(kind: str, course: Course, row: EntryRow | None, applied: bool) -> str:
    label = f"{course.day_period} {course.name}"
    if kind == "available":
        msg = f"🟢 **空きが出ました** {label}\n申込数/定員: {_seats(row)}"
        if applied:
            msg += "\n→ 自動で申込を送信しました。KULASISで結果を確認してください。"
        return msg
    if kind == "closed":
        return f"🔴 満席に戻りました: {label}（{_seats(row)}）"
    return f"⚠️ 検索結果に見つかりません: {label}（科目名/曜時限の不一致、または対象外の可能性）"


def run(args: argparse.Namespace) -> int:
    courses = load_courses()
    cfg = load_config()

    if args.offline_html:  # 保存したHTMLでパーサだけ確認する
        raw = Path(args.offline_html).read_bytes()
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("cp932", errors="replace")
        rows = parse_entrylimit(html)
        print(f"{len(rows)} 行を解析")
        for c in courses:
            row = find_row(c, rows)
            print(f"{c.key}: {status_of(row)}  {_seats(row)}  lecture_no={row.lecture_no if row else None}")
        return 0

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")

    def notify(msg: str) -> None:
        if args.dry_run:
            print(f"[dry-run] {msg}")
        else:
            send_discord(webhook, msg)

    if args.test_discord:
        send_discord(webhook, "✅ kulasis-waitlist: テスト通知")
        return 0

    state = load_state()
    auto_apply = bool((cfg.get("apply") or {}).get("auto_apply", False))

    try:
        user, password = os.environ["KULASIS_USER"], os.environ["KULASIS_PASSWORD"]
    except KeyError as e:
        print(f"環境変数 {e} が未設定です", file=sys.stderr)
        return 2

    totp_secret = os.environ.get("TOTP_SECRET")

    try:
        client = KulasisClient(cfg)
        client.login(user, password, totp_secret=totp_secret)
        html = client.fetch_entrylimit_page()
        if args.dry_run: #デバック保存は --dry-run 実行時のみ
            Path("debug_fetched.html").write_text(html, encoding="utf-8")
        rows = parse_entrylimit(html)
    except (KulasisError, requests.RequestException) as e:
        msg = f"{type(e).__name__}: {e}"
        print(msg, file=sys.stderr)
        if state.get("last_error") != msg:  # 同じエラーは1回だけ通知
            notify(f"❌ KULASIS監視エラー: {msg}")
            state["last_error"] = msg
            if not args.dry_run:
                save_state(state)
        return 0

    for c in courses:
        row = find_row(c, rows)
        status = status_of(row)
        entry = state["courses"].get(c.key) or {}
        old_status = entry.get("status")
        already_applied = bool(entry.get("applied"))
        kind = transition_kind(old_status, status)

        kulasis_applied = row.already_applied if row is not None else False
        print(
            f"{c.key}: {old_status} -> {status} {_seats(row)} "
            f"[申込済み: KULASIS側={kulasis_applied} / state記録={already_applied}, "
            f"auto_apply設定={auto_apply}]"
        )

        applied_now = False
        # 状態の「変化」ではなく「今available かつ未申込か」で判定する。
        # これにより、既にavailableな状態が続いている科目も毎回申込を試みる。
        should_try_apply = (
            auto_apply
            and status == "available"
            and row is not None
            and row.lecture_no
            and not row.already_applied
            and not already_applied
            and not args.dry_run
        )
        if should_try_apply:
            try:
                client.apply(row.lecture_no)
                applied_now = True
                already_applied = True
                print(f"{c.key}: 自動申込 成功")
            except (KulasisError, requests.RequestException) as e:
                print(f"{c.key}: 自動申込 失敗 {type(e).__name__}: {e}")
                notify(f"❌ {c.day_period} {c.name} の自動申込に失敗: {type(e).__name__}: {e}")

        if kind:
            notify(build_message(kind, c, row, applied_now))
        elif applied_now:
            # 状態遷移は無かったが、今回のポーリングで新たに自動申込できた場合も通知する
            notify(build_message("available", c, row, applied_now))

        entry["status"] = status
        if already_applied or (row is not None and row.already_applied):
            entry["applied"] = True
        state["courses"][c.key] = entry

    live = {c.key for c in courses}
    state["courses"] = {k: v for k, v in state["courses"].items() if k in live}
    state["last_error"] = None
    if not args.dry_run:
        save_state(state)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="KULASIS 履修(人数)制限 監視・自動申込")
    p.add_argument("--dry-run", action="store_true", help="通知・state保存・自動申込をせず標準出力に出す")
    p.add_argument("--test-discord", action="store_true", help="Discordにテスト通知だけ送る")
    p.add_argument("--offline-html", metavar="FILE", help="保存済みHTMLでパーサだけ確認する")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
