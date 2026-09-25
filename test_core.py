"""パーサ・照合・状態遷移のテスト。HTMLは実サイトのものではなく、実ページの列構成を模した合成データ。"""
from src.config import Course, norm
from src.main import build_message, find_row, status_of
from src.parser import parse_entrylimit
from src.state import transition_kind

ROW_TMPL = """
<tr class="odd_normal">
  <td>{status}</td>
  <td>{day_period}</td>
  <td style="text-align: left;">{name}</td>
  <td>{teacher}</td>
  <td>後期</td>
  <td>人社群</td>
  <td>Ａ群</td>
  <td>{seats}</td>
  <td style="text-align: left;">定員を超えた場合、無作為抽選を行う。</td>
  <td style="text-align: left;">
    <form method="post" action="regist_check">
      <input type="hidden" name="entrylimitLectureNo" value="{lno}">
      <input type="image" src="/img/button_mini_mousikomi.gif" alt="申込">
    </form>
  </td>
</tr>
"""

HEADER = """
<table><tbody>
<tr class="th_normal">
  <td>状態</td><td>曜時限</td><td>科目名</td><td>担当教員</td><td>開講期</td>
  <td>群</td><td>旧群</td><td>申込数/定員</td><td>抽選方法</td><td>申込</td>
</tr>
"""

SAMPLE = HEADER + "".join([
    ROW_TMPL.format(status="", day_period="月2", name="人文地理学", teacher="甲", seats="29/30", lno="1001"),
    ROW_TMPL.format(status="", day_period="火2", name="宗教学Ⅱ", teacher="乙", seats="40/40", lno="1002"),
    ROW_TMPL.format(status="申込済", day_period="金4", name="社会学II", teacher="丙", seats="10/10", lno="1003"),
]) + "</tbody></table>"


def test_norm_absorbs_roman_numerals_and_width():
    assert norm("宗教学Ⅱ") == norm("宗教学II") == "宗教学II"


def test_parse_entrylimit():
    rows = parse_entrylimit(SAMPLE)
    assert len(rows) == 3
    assert rows[0].applicants == 29 and rows[0].capacity == 30
    assert rows[0].lecture_no == "1001"
    assert rows[2].already_applied is True


def test_find_row_and_status():
    rows = parse_entrylimit(SAMPLE)
    c1 = Course("人文地理学", "月", 2)
    c2 = Course("宗教学II", "火", 2)
    c3 = Course("言語学II", "木", 3)
    assert status_of(find_row(c1, rows)) == "available"
    assert status_of(find_row(c2, rows)) == "full"
    assert status_of(find_row(c3, rows)) == "not_found"


def test_already_applied_flag():
    rows = parse_entrylimit(SAMPLE)
    c = Course("社会学II", "金", 4)
    row = find_row(c, rows)
    assert row.already_applied is True


def test_transitions():
    assert transition_kind(None, "available") == "available"
    assert transition_kind("full", "available") == "available"
    assert transition_kind("available", "available") is None
    assert transition_kind(None, "full") is None
    assert transition_kind("available", "full") == "closed"
    assert transition_kind(None, "not_found") == "warn"


def test_build_message_mentions_auto_apply():
    rows = parse_entrylimit(SAMPLE)
    c = Course("人文地理学", "月", 2)
    row = find_row(c, rows)
    msg = build_message("available", c, row, applied=True)
    assert "自動で申込" in msg
