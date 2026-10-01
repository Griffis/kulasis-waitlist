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
