#!/usr/bin/env python3
# test-lane: fast
"""DGN-1910 C16: routines/lib/secret_scan.py (locked spec 3.5).

  S1  positive + negative fixture per category, body and strict
  S2  strict-only: the bare keyword hits in strict, never in body
  S3  the grill scratch inputs all hit (AWS key, sk-ant-, ghp_, a KR resident
      id, keyword + value, a webhook URL, a Luhn-valid card)
  S4  redact(): every body hit replaced; a Telegram token stays '<token>'
      (mint_flow.mask parity); clean text byte-identical
  S5  CLI: rc 0 clean / 1 hits / 2 usage; output is 'cat file:line' and
      never carries the matched text; binary files skipped
  S6  charset: U+200B, U+2028, U+E000, a stray CR -> hit; tab/newline ok
  S7  census: every shipped SKILL.md (skills/, agents/.template/) passes the
      body scan and the charset check -- 0 false positives

Run: python3 agents/.template/routines/tests/test-dgn1910-secret-scan.py
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "..", "lib")
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, LIB)
import secret_scan as ss  # noqa: E402

CLI = os.path.join(LIB, "secret_scan.py")

# Fixtures are built at runtime so this file never carries a key-shaped
# literal itself (a repo-wide sweep must not trip on the test).
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
SK_ANT = "sk-" + "ant-api03-" + "abcdefghijklmnopqrstuv"
SK = "sk-" + "proj" + "Abcdefghij0123456789"
GHP = "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz0123456789"
GHO = "gho" + "_" + "abcdefghijklmnop1234"
GH_PAT = "github" + "_pat_" + "11ABCDEFG0123456789_abcdef"
XOX = "xox" + "b-" + "1234567890-abcdefghij"
AIZA = "AIza" + "SyA1234567890abcdefghijklmnopqrstuv"
PEM = "-----" + "BEGIN RSA PRIVATE KEY-----"
TG = "123456789" + ":" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
ENTROPIC = "wJalrXUtnFEMI" + "K7MDENGbPxRfiCYEXAMPLEKEY9q"
RRN = "950101" + "-" + "1234567"
CARD = "4111 1111" + " 1111 1111"
SLACK = "https://hooks.slack" + ".com/services/T000/B000/XXXX"
DISCORD = "https://discord" + ".com/api/webhooks/123/abc"
BIBEON_EUN = "\ube44\ubc88\uc740 hunter2"            # 'bibeon-eun hunter2'
GYEJWA_GA = "\uacc4\uc88c\uac00 \ud544\uc694\ud558\uba74"  # 'gyejwa-ga piryohamyeon'

POSITIVE = [
    ("key_prefix", "key " + AWS + " here"),
    ("key_prefix", SK_ANT),
    ("key_prefix", "x " + SK),
    ("key_prefix", GHP),
    ("key_prefix", GHO),
    ("key_prefix", GH_PAT),
    ("key_prefix", XOX),
    ("key_prefix", AIZA),
    ("key_prefix", PEM),
    ("telegram_token", "bot " + TG),
    ("high_entropy", "tok " + ENTROPIC + " end"),
    ("kr_rrn", "id " + RRN),
    ("kr_rrn", "id 9501011234567"),
    ("card", "card " + CARD),
    ("card", "4111-1111-1111-1111"),
    ("webhook", SLACK),
    ("webhook", DISCORD),
    ("keyword_value", BIBEON_EUN),
    ("keyword_value", "password: abc"),
    ("keyword_value", "pin 1234"),
    ("keyword_value", "otp=889900"),
]

NEGATIVE = [
    ("key_prefix", "task-list and desk-lamp, sk-short"),
    ("key_prefix", "ghp_x"),
    ("telegram_token", "12:30 meeting"),
    ("high_entropy", "__PROJECT_ROOT__/bridge/self_restart.sh"),
    ("high_entropy", "DGN-1505-accept-path/verification/selftest.sh"),
    ("high_entropy", "a" * 40),
    ("high_entropy", "0123456789abcdef" * 4),        # hex sha, entropy <= 4
    ("kr_rrn", "19501011234567 is 14 digits"),
    ("kr_rrn", "950101-9234567"),                      # gender digit 9
    ("card", "4111 1111 1111 1112"),                   # Luhn fails
    ("card", "123456789012"),                          # 12 digits
    ("webhook", "https://slack.com/intl/en"),
    ("keyword_value", "does NOT pin exact scopes"),
    ("keyword_value", "--password-stdin"),
    ("keyword_value", "spinning wheels"),
    ("keyword_value", "a re-pin: leave it"),
    ("keyword_value", GYEJWA_GA),
    ("keyword_value", "password"),
]


def cats(text, mode="body"):
    return {h.cat for h in ss.scan(text, mode)}


class Categories(unittest.TestCase):
    def test_s1_positive(self):
        for cat, text in POSITIVE:
            with self.subTest(cat=cat, idx=POSITIVE.index((cat, text))):
                self.assertIn(cat, cats(text, "body"))
                self.assertIn(cat, cats(text, "strict"))

    def test_s1_negative(self):
        for cat, text in NEGATIVE:
            with self.subTest(cat=cat, idx=NEGATIVE.index((cat, text))):
                self.assertNotIn(cat, cats(text, "body"))

    def test_s1_hits_are_offsets(self):
        text = "a " + AWS + " b"
        (h,) = [h for h in ss.scan(text, "body") if h.cat == "key_prefix"]
        self.assertEqual(text[h.start:h.end], AWS)

    def test_s2_strict_bare_keyword(self):
        for word in ("password", "PIN", "otp", "\ube44\ubc88", "\uacc4\uc88c"):
            with self.subTest(word=word):
                self.assertIn("keyword", cats("my " + word, "strict"))
                self.assertEqual(cats("my " + word, "body"), set())
        self.assertNotIn("keyword", cats("spinning", "strict"))
        self.assertRaises(ValueError, ss.scan, "x", "loose")

    def test_s3_grill_inputs(self):
        for text in (AWS, SK_ANT, GHP, RRN, BIBEON_EUN, SLACK, CARD):
            with self.subTest(idx=text[:4]):
                self.assertTrue(ss.scan(text, "body"))

    def test_s4_redact(self):
        self.assertEqual(ss.redact("bot " + TG + " ok"), "bot <token> ok")
        out = ss.redact("k " + AWS + " / " + BIBEON_EUN)
        self.assertNotIn(AWS, out)
        self.assertNotIn("hunter2", out)
        self.assertIn("\ube44\ubc88\uc740 <redacted>", out)
        clean = "plain owner text \ub17c\ubb38 \ub9c8\uac10"
        self.assertEqual(ss.redact(clean), clean)
        self.assertEqual(ss.redact(""), "")
        for _cat, text in POSITIVE:
            self.assertEqual(ss.scan(ss.redact(text), "body"), [], text[:6])

    def test_s6_charset(self):
        for ch, cat in (("\u200b", "Cf"), ("\u2028", "Zl"), ("\u2029", "Zp"),
                        ("\ue000", "Co"), ("\r", "Cc"), ("\x07", "Cc"), ("\ufeff", "Cf")):
            with self.subTest(cp=hex(ord(ch))):
                self.assertEqual(ss.bad_chars("a" + ch + "b"), [(1, cat)])
        self.assertEqual(ss.bad_chars("tab\there\nnext \ub17c\ubb38"), [])


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dgn1910-ss-")
        self.addCleanup(self.tmp.cleanup)

    def _file(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    def _run(self, *args):
        return subprocess.run([sys.executable, CLI] + list(args), capture_output=True, text=True)

    def test_s5_cli(self):
        clean = self._file("clean.md", "nothing here\n")
        dirty = self._file("dirty.md", "line one\nkey " + AWS + "\n" + BIBEON_EUN + "\n")
        binary = self._file("blob.bin", b"\0" + AWS.encode())
        r = self._run("--mode", "body", clean, binary)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = self._run("--mode", "body", clean, dirty)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.splitlines(),
                         ["key_prefix %s:2" % dirty, "keyword_value %s:3" % dirty])
        self.assertNotIn(AWS, r.stdout + r.stderr)
        self.assertNotIn("hunter2", r.stdout + r.stderr)
        r = self._run("--mode", "strict", self._file("kw.md", "my password\n"))
        self.assertEqual(r.returncode, 1)
        for bad in ([], ["--mode", "loose", clean], ["--mode", "body"],
                    ["--mode", "body", os.path.join(self.tmp.name, "absent")]):
            self.assertEqual(self._run(*bad).returncode, 2, bad)


class Census(unittest.TestCase):
    def test_s7_shipped_skill_md_zero_hits(self):
        found = []
        for top in ("skills", os.path.join("agents", ".template")):
            for base, dirs, files in os.walk(os.path.join(REPO, top)):
                dirs[:] = [d for d in dirs if d != ".git"]
                if "SKILL.md" in files:
                    found.append(os.path.join(base, "SKILL.md"))
        self.assertGreater(len(found), 10)
        bad = []
        for p in found:
            text, skip = ss.read_text(p)
            self.assertIsNone(skip, p)
            for h in ss.scan(text, "body"):
                bad.append("%s %s:%d" % (h.cat, os.path.relpath(p, REPO), ss.line_of(text, h.start)))
            for off, cat in ss.bad_chars(text):
                bad.append("%s %s:%d" % (cat, os.path.relpath(p, REPO), ss.line_of(text, off)))
        self.assertEqual(bad, [], "false positives on the shipped canon")


if __name__ == "__main__":
    unittest.main(verbosity=1)
