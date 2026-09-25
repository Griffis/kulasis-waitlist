"""courses.yml / config.yml の読み込み。"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
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
