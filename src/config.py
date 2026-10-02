"""courses.yml / config.yml の読み込み。"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DAYS = "月火水木金土"


def norm(s: str) -> str:
    """NFKC正規化 + 空白除去。Ⅱ→II、全角英数字→半角などの表記ゆれを吸収する。"""
    return "".join(unicodedata.normalize("NFKC", s).split())


@dataclass(frozen=True)
class Course:
    name: str
    day: str
    period: int
    match: str = ""  # 空なら name を使う

    @property
    def key(self) -> str:
        return f"{self.day}{self.period}:{self.name}"

    @property
    def day_period(self) -> str:
        """KULASISページの「曜時限」列の表記（例: "月2"）と比較する文字列。"""
        return f"{self.day}{self.period}"

    @property
    def needle(self) -> str:
        return norm(self.match or self.name)


def load_courses(path: Path = ROOT / "courses.yml") -> list[Course]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    courses: list[Course] = []
    for i, item in enumerate(data.get("courses") or [], 1):
        try:
            name = str(item["name"]).strip()
            day = str(item["day"]).strip()
            period = int(item["period"])
            match = str(item.get("match") or "").strip()
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"courses.yml の{i}件目が不正です: {item!r}") from e
        if not name:
            raise ValueError(f"courses.yml の{i}件目: name が空です")
        if len(day) != 1 or day not in DAYS:
            raise ValueError(f"courses.yml の{i}件目: day は {'/'.join(DAYS)} のどれか: {day!r}")
        if not 1 <= period <= 6:
            raise ValueError(f"courses.yml の{i}件目: period は1〜6: {period}")
        courses.append(Course(name=name, day=day, period=period, match=match))
    if not courses:
        raise ValueError("courses.yml に科目がありません")
    return courses


def load_config(path: Path = ROOT / "config.yml") -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# 動作モード（実行環境ごとの設定）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Profile:
    """動作モード。状況に合わせて、ページ遷移の数や待ち時間を切り替える。"""
    name: str
    description: str
    warmup: bool                      # 検索の前に、履修登録トップ・時間割ページを開くか（ブラウザの遷移の再現）
    timeslot_read_timeout_sec: float  # 履修登録ページの応答を待つ秒数（混雑時は順番待ちで遅い）
    course_gap_sec: float             # 科目ごとの検索の間隔（秒）


PROFILES: dict[str, Profile] = {
    # 通常運用: 定期実行でキャンセル待ちを監視する。負荷が軽いので、動作確認済みの手順(事前取得あり)で動く
    "watch": Profile("watch", "キャンセル待ちの監視（定期実行・通常時）", True, 120.0, 1.0),
    # 公開直後: 混雑して順番待ちになる。ページ遷移を最小にし、応答を長く待つ
    "rush": Profile("rush", "先着順の公開直後（混雑・ページ遷移を最小化）", False, 240.0, 0.0),
}
DEFAULT_MODE = "watch"
_OVERRIDABLE = {"warmup", "timeslot_read_timeout_sec", "course_gap_sec"}


def resolve_profile(cfg: dict, cli_mode: str | None = None, env_mode: str | None = None) -> Profile:
    """動作モードを決める。優先順位: --mode > 環境変数 KULASIS_MODE > config.yml の mode > watch。

    config.yml の modes.<モード名> で、モードの設定を個別に上書きできる。
    """
    name = str(cli_mode or env_mode or cfg.get("mode") or DEFAULT_MODE).strip().lower()
    if name not in PROFILES:
        raise ValueError(f"mode は {' / '.join(PROFILES)} のどれか: {name!r}")
    overrides = dict((cfg.get("modes") or {}).get(name) or {})
    unknown = set(overrides) - _OVERRIDABLE
    if unknown:
        raise ValueError(f"config.yml の modes.{name} に未知の項目: {sorted(unknown)}（使える項目: {sorted(_OVERRIDABLE)}）")
    if "warmup" in overrides:
        overrides["warmup"] = bool(overrides["warmup"])
    for key in ("timeslot_read_timeout_sec", "course_gap_sec"):
        if key in overrides:
            overrides[key] = float(overrides[key])
    return replace(PROFILES[name], **overrides)