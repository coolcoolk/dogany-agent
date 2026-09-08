#!/usr/bin/env python3
# test-lane: fast    # no subprocess, no network (DGN-1263 marker requirement)
"""test-baseline-authoring-lint.py -- tests for routines/baseline-authoring-lint.py

pytest-style (assert-based `test_*` functions) AND plain-python3 runnable:
    python3 routines/tests/test-baseline-authoring-lint.py
    pytest routines/tests/test-baseline-authoring-lint.py
Exit 0 = all pass, 1 = any fail.

Covers, per the build spec:
  - every exemption: the four persona data values, fenced code block,
    blockquote, SKILL.md `description:` frontmatter
  - a true positive of EACH of the two checks, separately
  - the "enforcer breaks its own rule" case as a REAL-CORPUS fixture:
    .claude/agents/baseline-editor.md is read off disk, not synthesised.

FLEET NOTE (2026-09-08 port to agents/.template)
  The real-corpus assertions are written against WHAT THE INSTANCE ACTUALLY
  CARRIES, not against one workspace's copy. The original single-instance
  version asserted the exemption set was exactly {KOREAN_IN_PROSE,
  PROVENANCE_DATE}; measured 2026-09-08, the canonical baseline-editor.md
  (identical md5 across the template and every domain instance) quotes only
  the date shape, so it yields PROVENANCE_DATE alone. The invariant that
  actually matters -- the enforcer produces ZERO FINDINGS, and every hit it
  does produce is accounted for as an inline quote -- is asserted instead of
  the count. A file the instance does not carry is an explicit SKIP line, never
  a silent pass.
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
LINT_PATH = os.path.join(os.path.dirname(HERE), "baseline-authoring-lint.py")

_spec = importlib.util.spec_from_file_location("baseline_authoring_lint",
                                               LINT_PATH)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

PERSONA = "identity/hot.custom.agent.md"
SKILL = ".claude/skills/example-skill/SKILL.md"
ENFORCER = ".claude/agents/baseline-editor.md"

SKIPPED = []

# --- instance-language fixtures -------------------------------------------
# Escaped, not literal: framework code is English ASCII only. Each gloss says
# what the fixture means so the tests stay readable.

# a rule sentence: 'safe, reversible work runs immediately'
KO_RULE = ("안전하고 가역적인 "
           "작업은 즉시 실행한다.")
# the owner form of address (one of the four exempt values)
KO_OWNER = "형님"
# an agent name value (one of the four exempt values) -- fictional placeholder
KO_NAME = "김보라"
# a rule sentence sitting inside Relationship:
# 'fix user typos silently and proceed'
KO_TYPO_RULE = ("유저 오타는 조용히 "
                "고쳐서 진행한다.")
# a display_name value: 'example skill'
KO_DISPLAY = "예시 스킬"
# a user trigger utterance: 'run the harvest'
KO_TRIGGER = "수확 돌려줘"
# a body rule sentence: 'the harvest runs daily at 07:30'
KO_BODY_RULE = ("수확은 매일 07:30에 "
                "돌린다.")
# 'this line is not exempt'
KO_NOTES = ("이 줄은 면제 대상이 "
            "아니다")
# a CLI prompt argument: 'one retro question for today'
KO_PROMPT = "오늘 회고 질문 하나"
# 'this line really is body prose'
KO_REAL_BODY = ("이 줄은 진짜 "
                "본문이다.")
# 'the owner said: run it immediately'
KO_QUOTED_SAID = ("형님이 말했다: 즉시 "
                  "실행해라")
# a tone candidate label: 'strong and direct style'
KO_TONE_LABEL = ("강하고 직접적인 "
                 "스타일")
# its nested sample utterance: 'no reason to skip today.'
KO_TONE_SAMPLE = ("오늘 훈련 빠질 이유 "
                  "없습니다.")
# 'the file layout is invariant' -- real shape of an indented continuation
KO_CONT = "파일 배치는 불변"


def _run(text, rel_path=SKILL):
    findings, exemptions = lint.analyse_text(text, rel_path)
    ko = [f["line"] for f in findings if f["check"] == "KOREAN_IN_PROSE"]
    dt = [f["line"] for f in findings if f["check"] == "PROVENANCE_DATE"]
    return ko, dt, exemptions


def _reasons(exemptions, check=None):
    return sorted({e["reason"] for e in exemptions
                   if check is None or e["check"] == check})


# --------------------------------------------------------------------------
# true positives -- one per check, reported separately
# --------------------------------------------------------------------------

def test_true_positive_korean_in_prose():
    ko, dt, _ = _run("- %s\n" % KO_RULE)
    assert ko == [1], ko
    assert dt == [], dt


def test_true_positive_provenance_date():
    ko, dt, _ = _run("- Push to the public mirror needs owner approval "
                     "(owner ruling dec-035, 2026-07-16).\n")
    assert dt == [1], dt
    assert ko == [], ko


def test_checks_stay_separate_on_one_line():
    """A line can violate both. They are counted once each, never merged."""
    ko, dt, _ = _run("- %s %s (%s 2026-08-20).\n"
                     % (KO_RULE, KO_OWNER, KO_OWNER))
    assert ko == [1], ko
    assert dt == [1], dt


def test_bare_date_without_attributor_is_not_a_stamp():
    """The rule is about provenance STAMPS. A date with nobody credited is
    not one -- e.g. a sample timestamp in a command line."""
    _, dt, _ = _run("- retention window starts 2026-07-16 and rolls monthly\n")
    assert dt == [], dt


def test_encoding_name_plus_date_is_not_a_stamp():
    """Regression from the 2026-09-08 port. A generic `[A-Z]{2,5}-\\d+` ticket
    shape was tried for the attributor column and matched "utf-8", "sha-256"
    and "ISO-8601", so any line naming an encoding next to a date became a
    phantom stamp. The attributor list stays explicit."""
    for line in ("- files are read as utf-8 since 2026-07-16\n",
                 "- the digest is sha-256, rotated 2026-07-16\n",
                 "- timestamps are ISO-8601 as of 2026-07-16\n"):
        _, dt, _ = _run(line)
        assert dt == [], (line, dt)


# --------------------------------------------------------------------------
# exemption 1 -- the four persona data values
# --------------------------------------------------------------------------

def test_exempt_persona_four_values():
    text = (
        "## Identity\n"
        "- Name: **%s (Bora Kim)**\n"
        "## Relationship\n"
        "- Address the user as %s (Korean).\n"
        "- Speak Korean. Tone: clean + polite. Humor: 10%%.\n"
        % (KO_NAME, KO_OWNER)
    )
    ko, _, ex = _run(text, PERSONA)
    assert ko == [], ko
    assert lint.R_PERSONA_VALUE in _reasons(ex), _reasons(ex)


def test_exempt_persona_four_values_template_phrasing():
    """The template and other domain instances write the same four values in
    DIFFERENT words than the original single-instance version does. Measured
    2026-09-08: with only the original phrasing listed, the exemption
    matched nothing on agents/.template and the lint reported
    every persona DATA VALUE as an authoring violation."""
    text = (
        "## Identity\n"
        "- Name: **%s**\n"
        "## Relationship\n"
        "- Call the user **%s**. Speak **ko**.\n"
        "- Tone: %s. Humor: 70%%\n"
        % (KO_NAME, KO_OWNER, KO_TONE_LABEL)
    )
    ko, _, ex = _run(text, PERSONA)
    assert ko == [], ko
    assert lint.R_PERSONA_VALUE in _reasons(ex), _reasons(ex)


def test_persona_exemption_is_value_level_not_section_level():
    """baseline-editor.md -- a section name is NOT a carve-out. A rule line
    sitting inside Relationship is still workflow prose."""
    text = (
        "## Relationship\n"
        "- Address the user as %s (Korean).\n"
        "- %s\n" % (KO_OWNER, KO_TYPO_RULE)
    )
    ko, _, _ = _run(text, PERSONA)
    assert ko == [3], ko


def test_persona_exemption_does_not_leak_to_other_files():
    """The four-value exemption is keyed to the persona file only."""
    ko, _, _ = _run("- Name: **%s (Bora Kim)**\n" % KO_NAME, SKILL)
    assert ko == [1], ko


# --------------------------------------------------------------------------
# exemption 2 -- SKILL.md frontmatter description (+ display_name)
# --------------------------------------------------------------------------

def test_exempt_skill_description_frontmatter():
    text = (
        "---\n"
        "name: example-skill\n"
        "display_name: %s\n"
        "description: Fires on \"%s\", \"harvest feedback\".\n"
        "---\n"
        "# body\n" % (KO_DISPLAY, KO_TRIGGER)
    )
    ko, _, ex = _run(text)
    assert ko == [], ko
    reasons = _reasons(ex)
    assert any(r.startswith(lint.R_FRONTMATTER) for r in reasons), reasons


def test_frontmatter_exemption_ends_with_frontmatter():
    """Body prose after the closing --- is back in scope."""
    text = (
        "---\n"
        "name: example-skill\n"
        "description: Fires on \"%s\".\n"
        "---\n"
        "- %s\n" % (KO_TRIGGER, KO_BODY_RULE)
    )
    ko, _, _ = _run(text)
    assert ko == [5], ko


def test_non_exempt_frontmatter_key_still_checked():
    text = (
        "---\n"
        "name: example-skill\n"
        "notes: %s\n"
        "---\n" % KO_NOTES
    )
    ko, _, _ = _run(text)
    assert ko == [3], ko


# --------------------------------------------------------------------------
# exemption 3 -- quoted regions
# --------------------------------------------------------------------------

def test_exempt_fenced_code_block():
    text = (
        "- run the push helper:\n"
        "```\n"
        "routines/push.sh --prompt %s\n"
        "owner spec 2026-07-14\n"
        "```\n"
        "- %s\n" % (KO_PROMPT, KO_REAL_BODY)
    )
    ko, dt, ex = _run(text)
    assert ko == [6], ko
    assert dt == [], dt
    assert lint.R_FENCE in _reasons(ex), _reasons(ex)


def test_exempt_blockquote():
    text = (
        "> %s (%s 2026-08-20)\n"
        "- the rule itself is written in English.\n"
        % (KO_QUOTED_SAID, KO_OWNER)
    )
    ko, dt, ex = _run(text)
    assert ko == [] and dt == [], (ko, dt)
    assert lint.R_BLOCKQUOTE in _reasons(ex), _reasons(ex)


def test_exempt_inline_quote():
    text = "- trigger utterances: \"%s\", \"harvest feedback\"\n" % KO_TRIGGER
    ko, _, ex = _run(text)
    assert ko == [], ko
    assert lint.R_INLINE_QUOTE in _reasons(ex), _reasons(ex)


def test_escaped_quote_pairing():
    """A nested backslash-escaped sample must not leak its text back as
    prose. Real instance: dogany-user-onboarding/SKILL.md."""
    text = "   \"%s -- \\\"%s\\\"\"\n" % (KO_TONE_LABEL, KO_TONE_SAMPLE)
    ko, _, _ = _run(text)
    assert ko == [], ko


def test_indented_continuation_is_not_a_code_block():
    """Divergence from attrib_gate._strip_quoted, deliberate. Real instance:
    dogany-lifekit-setup/SKILL.md has an indented bullet continuation that
    carries authored instance-language text; treating indentation as a code
    fence would silently drop it."""
    text = ("- never delete the generic-brief\n"
            "     plist file (it stays dormant on disk; C2: %s).\n" % KO_CONT)
    ko, _, _ = _run(text)
    assert ko == [2], ko


# --------------------------------------------------------------------------
# real-corpus fixtures
# --------------------------------------------------------------------------

def test_enforcer_document_is_clean_on_real_corpus():
    """.claude/agents/baseline-editor.md is the document that DECLARES both
    rules. It quotes the shapes it forbids -- "owner spec 2026-07-14" and, in
    some copies, the instance-language rendering of "owner directive
    YYYY-MM-DD". Both are the rule QUOTING the shape it forbids.

    A raw-text scan reports the enforcer as a violator. That finding is a
    FALSE POSITIVE, and it is the one this lint must not emit: the loudest
    finding being wrong is how a lint dies.

    Invariant asserted: ZERO findings, and every hit the enforcer does produce
    is accounted for as an inline quote. WHICH of the two checks fires is a
    property of the particular copy on disk, not of the lint -- measured
    2026-09-08, the canonical copy yields PROVENANCE_DATE only.
    """
    path = os.path.join(REPO_ROOT, ENFORCER)
    if not os.path.isfile(path):
        SKIPPED.append("test_enforcer_document_is_clean_on_real_corpus "
                       "(no %s in this instance)" % ENFORCER)
        return
    findings, exemptions = lint.analyse_file(ENFORCER, REPO_ROOT)
    assert findings == [], findings
    assert exemptions, ("the enforcer quotes at least one forbidden shape; "
                        "zero exemptions means the fixture stopped proving "
                        "anything")
    reasons = _reasons(exemptions)
    assert reasons == [lint.R_INLINE_QUOTE], reasons
    checks = sorted({e["check"] for e in exemptions})
    assert set(checks) <= {"KOREAN_IN_PROSE", "PROVENANCE_DATE"}, checks


def test_scan_set_resolves_on_real_corpus():
    """Every SCAN_SET entry this instance actually carries must resolve, and
    the resolver must never hand back a path it cannot open (live instances
    symlink their skills)."""
    files = lint.scan_files(REPO_ROOT)
    for rel in files:
        assert os.path.isfile(os.path.join(REPO_ROOT, rel)), rel
    for required in (PERSONA, "rules/hot.framework.md"):
        if os.path.isfile(os.path.join(REPO_ROOT, required)):
            assert required in files, (required, files)
        else:
            SKIPPED.append("scan_set entry %s absent in this instance"
                           % required)


def test_exit_code_is_always_zero():
    """report-only contract: findings must never become a nonzero exit."""
    rc = lint.main(["--repo-root", REPO_ROOT, "--quiet"])
    assert rc == 0, rc


def test_missing_scan_entries_are_not_an_error():
    """An instance layout that carries none of the scan set must produce an
    empty report and exit 0, not a traceback."""
    import tempfile
    d = tempfile.mkdtemp()
    try:
        assert lint.scan_files(d) == []
        assert lint.main(["--repo-root", d, "--quiet"]) == 0
    finally:
        import shutil
        shutil.rmtree(d)


# --------------------------------------------------------------------------

def _main():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            print("  FAIL: %s -- %s" % (name, exc))
            failed += 1
        except Exception as exc:  # noqa: BLE001 -- harness reports all
            print("  ERROR: %s -- %r" % (name, exc))
            failed += 1
        else:
            print("  PASS: %s" % name)
            passed += 1
    for line in SKIPPED:
        print("  SKIP: %s" % line)
    print("baseline-authoring-lint tests: %d passed, %d failed, %d skipped"
          % (passed, failed, len(SKIPPED)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
