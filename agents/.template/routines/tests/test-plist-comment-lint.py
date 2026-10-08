#!/usr/bin/env python3
# test-lane: fast    # no subprocess, no network
# test-covers: *.plist *.plist.tpl    # checks every authored plist surface
"""test-plist-comment-lint.py -- tests for routines/plist-comment-lint.py (DGN-1442)

Run: python3 routines/tests/test-plist-comment-lint.py
Exit 0 = all pass.
"""

import importlib.util
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)
_spec = importlib.util.spec_from_file_location(
    "plist_comment_lint", os.path.join(ROUTINES, "plist-comment-lint.py"))
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

TMPL = ('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>\n'
        '%s\n<key>Label</key><string>x</string></dict></plist>\n')


class TestPlistCommentLint(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write(self, name, comment):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(TMPL % comment)
        return path

    def test_clean_comment_passes(self):
        self.assertEqual(
            lint.lint_file(self._write("a.plist", "<!-- ok - single dash -->")),
            [])

    def test_double_dash_in_comment_flagged_with_line(self):
        found = lint.lint_file(
            self._write("b.plist", "<!-- fine -->\n<!-- bad -- here -->"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], 4)

    def test_trailing_dash_flagged(self):
        self.assertEqual(
            len(lint.lint_file(self._write("c.plist", "<!-- x --->"))), 1)

    def test_non_comment_parse_failure_flagged(self):
        path = os.path.join(self.tmp, "d.plist")
        with open(path, "w") as f:
            f.write("<plist><dict></plist>")
        found = lint.lint_file(path)
        self.assertEqual(len(found), 1)
        self.assertIn("plistlib parse failure", found[0][1])

    def test_main_exit_codes_and_dir_expand(self):
        nested = os.path.join(self.tmp, "bundle")
        os.makedirs(nested)
        self._write("ok.plist", "<!-- ok -->")
        self._write("bundle/ok.plist.tpl", "<!-- ok -->")
        self.assertEqual(lint.main([self.tmp]), 0)
        self._write("bundle/bad.plist.tpl", "<!-- a -- b -->")
        self.assertEqual(lint.main([self.tmp]), 1)

    def test_repo_routines_plists_and_templates_clean(self):
        self.assertEqual(lint.main([ROUTINES]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
