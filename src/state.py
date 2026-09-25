"""state.json の読み書きと、通知すべき状態遷移の判定。

state.json には状態(available/full/not_found)と申込済みフラグだけを保存する。
申込者数など毎回変わる値は保存しない(コミットが毎回発生するのを避けるため)。
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT

STATE_PATH = ROOT / "state.json"


def load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return {"courses": {}, "last_error": None}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("courses", {})
    data.setdefault("last_error", None)
    return data


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def transition_kind(old: str | None, new: str) -> str | None:
    """通知の種類を返す。None なら通知しない。

    - available : 空きが出た(初回観測で空きでも通知)
    - closed    : 空き → 満席
    - warn      : 科目が見つからない (状態が変わったときだけ)
    初回に満席だった場合は黙ってベースラインとして記録する。
    """
    if new == "available" and old != "available":
        return "available"
    if new == "full" and old == "available":
        return "closed"
    if new == "not_found" and old != "not_found":
        return "warn"
    return None
