"""ログ出力の共通部品。各行の先頭に「プロセス開始からの経過秒」を付ける。

- log(tag, msg)        : `[  3.21s] [タグ] 本文` の形式で出力する
- log_start()          : 開始時刻（JST と UTC）を1行出す。最初に1回だけ呼ぶ
- timed(tag, label)    : with 文で囲んだ処理の所要時間をログに出す（例外時も出して再送出する）

経過秒は time.monotonic()（システム時計の補正に影響されない単調増加時計）で測る。
1行あたりのコストは print の出力が支配的で、計測自体（monotonic 1回 + 整形）は無視できる。
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

T0 = time.monotonic()  # このモジュールの読み込み時点 ≒ プロセスの開始
_T0_WALL = datetime.now(timezone.utc)
JST = timezone(timedelta(hours=9))


def elapsed() -> float:
    """プロセス開始からの経過秒。"""
    return time.monotonic() - T0


def log(tag: str, msg: str, *, file=None) -> None:
    """`[経過秒] [タグ] 本文` の形式でログを出す。ID・sessid・パスワード等は出さないこと。"""
    print(f"[{elapsed():7.2f}s] [{tag}] {msg}", file=file or sys.stdout)


def log_start() -> None:
    """開始時刻をJSTとUTCで出す。以降の行の経過秒は、この時刻からの秒数。"""
    jst = _T0_WALL.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    utc = _T0_WALL.strftime("%H:%M:%S")
    log("開始", f"🕐 {jst} JST（{utc} UTC）。以降の行頭の秒数は、この時刻からの経過秒")


@contextmanager
def timed(tag: str, label: str):
    """with ブロックの所要時間を出す。成功は ⏱、例外は ❌ で出し、例外はそのまま送出する。"""
    started = time.monotonic()
    try:
        yield
    except BaseException:
        log(tag, f"❌ {label}: 失敗（{time.monotonic() - started:.2f}秒で中断）")
        raise
    log(tag, f"⏱ {label}: {time.monotonic() - started:.2f}秒")