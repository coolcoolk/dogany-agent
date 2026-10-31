"""DGN-1889: a raw markdown table with natural-language cells is sent as a
blockquote list, not as a monospace grid.

Measured 2026-10-06 17:11 KST (dev agent, owner screenshot 17:14): the agent
answered with a raw 2-column markdown table whose cells were Korean sentences.
DGN-775 wraps every confirmed table in <pre>, and CJK text never aligns in a
monospace block (Hangul is double-width, the pipes drift), so the owner got a
broken grid -- twice that day. The vendor contract (vendors/telegram.md,
Tables) already forbids raw tables and routes this content to Tier 2, the
blockquote list; the bridge now enforces that at the formatting seam instead
of relying on the model to remember.

Contract under test:
- a table whose cells carry natural language (any CJK character, or a data
  cell of 3+ words) -> one blockquote: a numbered head line per data row (its
  first cell) + one child line per remaining cell ("header: value"), every
  cell kept;
- a values-only table (numbers / short status tokens, Tier 1) keeps the
  DGN-775 <pre> block unchanged;
- a fenced table is code, never touched; pipe prose without a separator row
  is never a table.
"""

import unittest

from bridge.formatting import markdown_to_telegram_html

# The 17:11 answer's table, verbatim (6 rows, 2 columns, Korean cells).
REAL_TABLE = """| 파일 | 2.6.0에서 바뀐 이유 |
|---|---|
| 온보딩 검사 | 포트폴리오 자동 제안을 형님 결정으로 뺐습니다 ("기능빼") |
| 브리핑 시간 설정 | 예약 작업을 옮기는 방식을 새로 짰습니다 |
| 상태 표시줄 | 작업대를 맨 위에 두는 방식으로 바꿨습니다 |
| 업데이트 안내 | 알림 발송 기록 방식을 새로 바꿨습니다 |
| 업데이트 안내 테스트 | 2.6.0에도 있고 내용만 바뀌었습니다 |
| 기억 정리 | 새벽 정리를 시간 안에 끝내는 방식으로 다시 짰습니다 |"""

REAL_ANSWER = (
    "**확인한 결과: 걱정할 손실이 아닙니다**\n"
    "6개 파일의 코드는 까리가 따로 만든 게 아닙니다.\n\n"
    + REAL_TABLE
    + "\n\n업데이트하면 2.6.0의 새 코드로 바뀌는 것이라, 사라지는 기능은 없습니다."
)


class TestProseTableBecomesBlockquoteList(unittest.TestCase):
    def test_real_case_has_no_monospace_grid(self):
        html = markdown_to_telegram_html(REAL_ANSWER)
        self.assertNotIn("<pre>", html)
        self.assertNotIn("|---", html)
        self.assertNotIn("| 온보딩 검사 |", html)

    def test_real_case_is_one_blockquote_list_in_row_order(self):
        html = markdown_to_telegram_html(REAL_ANSWER)
        self.assertEqual(html.count("<blockquote>"), 1)
        quote = html.split("<blockquote>", 1)[1].split("</blockquote>", 1)[0]
        lines = quote.split("\n")
        self.assertEqual(
            lines[:4],
            [
                "1. 온보딩 검사",
                "  • 2.6.0에서 바뀐 이유: 포트폴리오 자동 제안을 형님 결정으로 뺐습니다 (\"기능빼\")",
                "2. 브리핑 시간 설정",
                "  • 2.6.0에서 바뀐 이유: 예약 작업을 옮기는 방식을 새로 짰습니다",
            ],
        )
        self.assertEqual(lines[-2], "6. 기억 정리")
        self.assertEqual(len(lines), 12)

    def test_every_cell_survives(self):
        html = markdown_to_telegram_html(REAL_ANSWER)
        for row in REAL_TABLE.split("\n")[2:]:
            for cell in row.strip("|").split("|"):
                self.assertIn(cell.strip(), html)

    def test_prose_around_the_table_is_untouched(self):
        html = markdown_to_telegram_html(REAL_ANSWER)
        self.assertTrue(html.startswith("<b>확인한 결과: 걱정할 손실이 아닙니다</b>\n"))
        self.assertTrue(html.endswith("\n\n업데이트하면 2.6.0의 새 코드로 바뀌는 것이라, 사라지는 기능은 없습니다."))

    def test_multi_column_rows_get_one_child_per_cell(self):
        text = (
            "| option | cost | risk |\n"
            "|---|---|---|\n"
            "| rebuild the index | two hours of downtime | data loss if interrupted |\n"
            "| patch in place | ten minutes | none known |"
        )
        html = markdown_to_telegram_html(text)
        self.assertEqual(
            html,
            "<blockquote>1. rebuild the index\n"
            "  • cost: two hours of downtime\n"
            "  • risk: data loss if interrupted\n"
            "2. patch in place\n"
            "  • cost: ten minutes\n"
            "  • risk: none known</blockquote>",
        )

    def test_inline_markup_in_cells_still_renders(self):
        text = "| 항목 | 설명 |\n|---|---|\n| **로그인** | `/login` 명령으로 다시 연결 |"
        html = markdown_to_telegram_html(text)
        self.assertIn("1. <b>로그인</b>", html)
        self.assertIn("  • 설명: <code>/login</code> 명령으로 다시 연결", html)

    def test_escaped_pipe_stays_inside_its_cell(self):
        text = "| 명령 | 뜻 |\n|---|---|\n| a \\| b | 둘 중 하나를 고릅니다 |"
        html = markdown_to_telegram_html(text)
        self.assertIn("1. a | b", html)
        self.assertIn("  • 뜻: 둘 중 하나를 고릅니다", html)

    def test_empty_cell_adds_no_child_line(self):
        text = "| 이름 | 메모 | 상태 |\n|---|---|---|\n| 까리 |  | 업데이트 대기 |"
        html = markdown_to_telegram_html(text)
        self.assertNotIn("메모:", html)
        self.assertIn("  • 상태: 업데이트 대기", html)


class TestValueTablesKeepTheCodeBlock(unittest.TestCase):
    def test_values_only_table_keeps_pre(self):
        text = (
            "| metric | value | status |\n"
            "|---|---|---|\n"
            "| cpu | 12% | ok |\n"
            "| disk | 81% | warn |"
        )
        html = markdown_to_telegram_html(text)
        self.assertTrue(html.startswith("<pre>| metric | value | status |"))
        self.assertNotIn("<blockquote>", html)

    def test_two_word_status_cells_are_still_values(self):
        text = "| ticket | state |\n|---|---|\n| T-101 | not started |\n| T-102 | in review |"
        self.assertIn("<pre>", markdown_to_telegram_html(text))

    def test_pipe_prose_without_separator_is_not_a_table(self):
        text = "| 이건 표가 아니라 그냥 파이프가 들어간 문장입니다 |"
        html = markdown_to_telegram_html(text)
        self.assertEqual(html, text)


if __name__ == "__main__":
    unittest.main()
