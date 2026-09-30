"""KULASISのHTML解析。

- parse_lecture_search : 履修登録ページの科目検索結果（先着順トグル）  ← 現在使用
- parse_entrylimit     : 履修(人数)制限ページ（旧方式・互換用）

lecture_search の列構成:
講義コード / 科目名 / 担当教員 / 曜時限 / 教室名 / 開講期 / 群 / 旧群 / 申込者数/定員 / 操作
空きがある行の「操作」列には <form action="candidate_add"> と hidden lectureNo がある。
満席の行は button_mini_add_disable.gif の画像だけで form が無い。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

SEAT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
DAY_RE = re.compile(r"^[月火水木金土].*\d")


@dataclass(frozen=True)
class EntryRow:
    status: str          # 旧ページの「状態」列。lecture_search には無いので空文字
    day_period: str      # 例: "月2"、複数コマは "火4・火5"
    name: str
    teacher: str
    applicants: int | None
    capacity: int | None
    lecture_no: str | None   # 申込(追加)に必要なID。満席行では None
    lecture_code: str | None = None  # 講義コード（例: N276001）

    @property
    def has_room(self) -> bool:
        return (
            self.applicants is not None
            and self.capacity is not None
            and self.applicants < self.capacity
        )

    @property
    def already_applied(self) -> bool:
        return bool(self.status.strip())


def _cell_texts(tds, n: int) -> list[str]:
    return [" ".join(td.get_text(" ", strip=True).split()) for td in tds[:n]]


def parse_lecture_search(html: str) -> list[EntryRow]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[EntryRow] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 10:
            continue  # 入れ子テーブル内の行・区切り行を除外
        texts = _cell_texts(tds, 10)
        code, name, teacher, day_period, seats = texts[0], texts[1], texts[2], texts[3], texts[8]
        m = SEAT_RE.search(seats)
        if not m or not DAY_RE.match(day_period):
            continue  # ヘッダ行などを除外
        hidden = tds[9].find("input", {"name": "lectureNo"})
        lecture_no = hidden["value"] if hidden and hidden.has_attr("value") else None
        rows.append(EntryRow(
            status="", day_period=day_period, name=name, teacher=teacher,
            applicants=int(m[1]), capacity=int(m[2]),
            lecture_no=lecture_no, lecture_code=code or None,
        ))
    return rows


def parse_entrylimit(html: str) -> list[EntryRow]:
    """旧: 履修(人数)制限ページ用。列: 状態/曜時限/科目名/担当教員/開講期/群/旧群/申込数/定員/抽選方法/申込"""
    soup = BeautifulSoup(html, "lxml")
    rows: list[EntryRow] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 10:
            continue
        texts = _cell_texts(tds, 10)
        status, day_period, name, teacher = texts[0], texts[1], texts[2], texts[3]
        m = SEAT_RE.search(texts[7])
        if not m or not DAY_RE.match(day_period):
            continue
        hidden = tds[9].find("input", {"name": "entrylimitLectureNo"})
        lecture_no = hidden["value"] if hidden and hidden.has_attr("value") else None
        rows.append(EntryRow(
            status=status, day_period=day_period, name=name, teacher=teacher,
            applicants=int(m[1]), capacity=int(m[2]), lecture_no=lecture_no,
        ))
    return rows