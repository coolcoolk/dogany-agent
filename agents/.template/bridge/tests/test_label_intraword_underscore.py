"""Button labels keep intraword underscores (owner rehearsal 2026-10-05:
the ALLOW_OUTSIDE_ONCE button arrived as ALLOWOUTSIDEONCE and the
outside-path approval never matched). Emphasis markup at word boundaries
is still stripped (DGN-775)."""
import unittest

from bridge.options import strip_markdown_label


class LabelIntrawordUnderscore(unittest.TestCase):
    def test_tokens_keep_underscores(self):
        for t in ("ALLOW_OUTSIDE_ONCE", "DENY_OUTSIDE", "my_file_name", "a_b"):
            self.assertEqual(strip_markdown_label(t), t)

    def test_emphasis_still_stripped(self):
        self.assertEqual(strip_markdown_label("_italic_"), "italic")
        self.assertEqual(strip_markdown_label("__bold__"), "bold")
        self.assertEqual(strip_markdown_label("a _b_ c"), "a b c")
        self.assertEqual(strip_markdown_label("**bold** `code`"), "bold code")


if __name__ == "__main__":
    unittest.main()
