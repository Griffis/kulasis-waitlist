"""履修(人数)制限ページ（student/la/entrylimit/regist）のHTML解析。

実際のページ（ユーザー提供のサンプルHTML）に基づく。列構成:
状態 / 曜時限 / 科目名 / 担当教員 / 開講期 / 群 / 旧群 / 申込数/定員 / 抽選方法 / 申込

「申込」列には <form action="regist_check"><input type="hidden" name="entrylimitLectureNo" value="NNNN"> がある。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

SEAT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


@dataclass(frozen=True)
class EntryRow:
    status: str          # 「状態」列。空でなければ既に申込/当選済みの可能性
    day_period: str       # 例: "月2"、複数コマは "火4・火5"
    name: str
    teacher: str
    applicants: int | None
    capacity: int | None
    lecture_no: str | None  # entrylimitLectureNo。申込に必要

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


def parse_entrylimit(html: str) -> list[EntryRow]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[EntryRow] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 10:
            continue
        texts = [" ".join(td.get_text(" ", strip=True).split()) for td in tds[:10]]
        status, day_period, name, teacher = texts[0], texts[1], texts[2], texts[3]
        seats = texts[7]
        m = SEAT_RE.search(seats)
        if not m or not re.match(r"^[月火水木金土].*\d", day_period):
            continue  # ヘッダ行や該当しない行を除外
        applicants, capacity = int(m[1]), int(m[2])
        hidden = tds[9].find("input", {"name": "entrylimitLectureNo"})
        lecture_no = hidden["value"] if hidden and hidden.has_attr("value") else None
        rows.append(EntryRow(
            status=status, day_period=day_period, name=name, teacher=teacher,
            applicants=applicants, capacity=capacity, lecture_no=lecture_no,
        ))
    return rows
