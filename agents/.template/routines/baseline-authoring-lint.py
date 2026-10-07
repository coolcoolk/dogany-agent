#!/usr/bin/env python3
"""baseline-authoring-lint.py -- report-only lint for the two BASELINE
AUTHORING rules that exist in .claude/agents/baseline-editor.md but had no
machine watching them.

RULES CHECKED (both declared in .claude/agents/baseline-editor.md, "WRITING
MANDATE" and "STAMP-LINT" -- open that file, this docstring paraphrases, it
does not replace it)

  KOREAN_IN_PROSE  ("WRITING MANDATE")
      Workflow / procedure prose in framework docs and agent defs is
      authored in English ASCII, always. The exemption unit is the VALUE,
      not the section: exactly four data values follow the instance
      language -- form of address, agent name, tone label,
      working-language label. "Judge each line on its own, never the
      section as a block."

  PROVENANCE_DATE  ("STAMP-LINT")
      A provenance stamp baked into a rule line ("owner spec 2026-07-14"
      style) is WARNING severity, default action move-to-worklog. "Owner
      directive date" is ALWAYS attribution, NEVER authority-chain. The
      authority-chain exemption list in that rule is explicitly empty.

The two checks are INDEPENDENT and are reported separately. They are never
merged into one count: they have different severities in the source rule
(mandate vs warning) and different remedies (rewrite in English vs move the
stamp to a worklog ticket). A merged number would hide both.

CONTRACT
  report-only. exit 0 ALWAYS, findings or not. NO blocking, ever.
  Reads files. Writes nothing.

  WHY report-only (this is a design decision, not an oversight): every
  corpus measured so far starts deep in the red. Measured 2026-09-08 with
  --quiet:
      agents/.template          korean_in_prose=25   provenance_date=1
      a domain instance A       korean_in_prose=2398 provenance_date=78
      a domain instance B       korean_in_prose=260  provenance_date=13
  Reproduce: python3 routines/baseline-authoring-lint.py --quiet
  A gate that fires on thousands of existing violations gets bypassed on its
  first day, and a habitually bypassed gate is a dead gate. That is measured
  in this estate, not hypothetical: routines/lib/attrib_gate.py cites a prior
  gate that died in 25 days by exactly this mechanism. Blocking becomes
  available only after a corpus is clean; until then the honest instrument is
  a number in a daily log.

QUOTED TEXT -- WHICH PASS USES WHICH INPUT (DGN-1331)
  attrib_gate.py runs two passes over two different texts: claim detection
  on _strip_quoted(body), source detection on the RAW body, because the
  citations pass 2 needs are exactly what pass 1 deletes.

  This lint has NO pass-2 equivalent. Neither check looks for a citation to
  be PRESENT; both ask whether a line ASSERTS something in its own voice --
  authored instance-language prose, or an attribution stamp the author baked
  into a rule. That is the same predicate shape as attrib_gate's pass 1, so
  both checks here run on STRIPPED text and nothing runs on raw.

  Concretely: baseline-editor.md itself contains the string
  "owner spec 2026-07-14" and, in some copies, the instance-language
  rendering of "owner directive YYYY-MM-DD". Both are the rule QUOTING the
  shape it forbids. A raw-text check reports the enforcer as breaking its
  own rule; that finding is a false positive, and a lint whose loudest
  finding is false dies. See routines/tests/test-baseline-authoring-lint.py
  for that fixture.

DIVERGENCE FROM attrib_gate._strip_quoted (deliberate, one rule dropped)
  attrib_gate strips 4-space / tab indented lines as indented code blocks.
  This lint does NOT. In these markdown docs a 4-space indent is bullet
  continuation prose, not a code block -- verified on a real skill file where
  an indented continuation line carries authored instance-language text.
  Under attrib_gate's rule that real finding disappears. Fenced blocks,
  blockquotes and inline quotes are stripped exactly as attrib_gate does.

SCOPE NOTE
  The persona file's "## Workflows" section is IN scope. baseline-editor.md
  is explicit that a section name is not a carve-out and that each line is
  judged on its own.

FLEET NOTE (ported to agents/.template 2026-09-08)
  Instance-agnostic by construction: the repo root is derived from this
  file's own location, every scan entry is a repo-relative glob, and a
  missing entry is simply absent from the report -- an instance with no
  .claude/agents/ or no skills produces a shorter file list, never an error.
  Live instances symlink their skills, so glob results are filtered by
  os.path.isfile (a dangling symlink is skipped, not opened), and an
  unreadable file is reported as skipped rather than aborting the run.
"""

import argparse
import glob
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

# --- scan set (module-level constant, per spec) ----------------------------
# Repo-relative paths / globs. Order is the report order. An entry that does
# not exist in this instance is skipped silently -- the set is a superset of
# what any one layout carries.
SCAN_SET = (
    "identity/hot.custom.agent.md",
    "rules/hot.framework.md",
    "rules/hot.custom.md",
    ".claude/agents/*.md",
    ".claude/skills/*/SKILL.md",
)

# Hangul syllables + jamo. Deliberately Hangul-only: the mandate names the
# INSTANCE language, and the instances this ships to are Korean-language.
# An instance whose working language is not Korean scores 0 on this check --
# the honest outcome, since the check has no opinion about that language yet.
# A generic "non-ASCII" test is NOT the fix: it would fire on every em-dash
# and arrow already used throughout these docs by design.
HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")

DATE_RE = re.compile(r"\b(?:19|20)\d{2}-\d{2}(?:-\d{2})?\b")

# An attribution marker: who/what the date is being credited to. A bare date
# with no attributor is not a provenance stamp (it can be a threshold, a
# filename, a sample timestamp) -- baseline-editor.md scopes the rule to
# stamps, so the attributor is required.
# The first two alternatives below are the instance-language tokens for the
# owner form of address and for the word "owner", written as escapes to keep
# this source ASCII (framework code rule).
# The ticket-id alternatives stay an explicit list. A generic
# `[A-Z]{2,5}-\d+` shape was tried during the 2026-09-08 port and measured to
# match "utf-8", "sha-256" and "ISO-8601", turning any line that carried an
# encoding name plus a date into a phantom stamp. dec-/DGN-/T- are estate-wide
# id shapes, not one instance's convention.
ATTRIBUTOR_RE = re.compile(
    r"\ud615\ub2d8|\uc624\ub108"
    r"|\bowner\b|\bowner-\w+"
    r"|\bdec-\d+|\bDGN-\d+|\bT-\d+"
    r"|\bapproved?\b|\bruling\b|\bdirective\b|\bLOCKED\b"
    r"|\blive incident\b|\bas ruled\b|\bper owner\b",
    re.IGNORECASE,
)

# --- exemptions ------------------------------------------------------------

# EXEMPTION 1 -- the four data values (baseline-editor.md).
# Value-level, not section-level: these patterns anchor to the specific
# lines that CARRY a value, so every other line in Identity/Relationship
# stays in scope. Keyed by repo-relative file: only the persona file
# declares instance-language values.
# FLEET GENERALISATION (2026-09-08): the original single-instance version
# carried only its own phrasing ("Address the user as ...", "Speak X.
# Tone: ..."). Measured the same day, agents/.template itself writes these
# four values as
#     - Name: ... / - Call the user ... / - Speak ... / - Tone: ...
# so on the template and on other domain instances the exemption matched
# NOTHING and the lint reported all four DATA VALUES as authoring
# violations -- the loudest possible false positive, on the one file every
# instance has. Both phrasings
# are listed; the exemption stays value-level and persona-file-only.
PERSONA_VALUE_EXEMPT = {
    "identity/hot.custom.agent.md": (
        # agent name
        re.compile(r"^\s*-\s*\*{0,2}Name\*{0,2}\s*:"),
        # form of address (how the agent addresses the user), both phrasings
        re.compile(r"^\s*-\s*Address the user as\b"),
        re.compile(r"^\s*-\s*Call the user\b"),
        # working-language label (may carry the tone label on the same line)
        re.compile(r"^\s*-\s*Speak\b"),
        # tone label on its own bullet
        re.compile(r"^\s*-\s*Tone\s*:"),
    ),
}

# EXEMPTION 2 -- frontmatter keys that carry instance-language values BY
# DESIGN. Verified against the skill-writing source of truth, which
# baseline-editor.md declares wins on conflict:
#   .claude/skills/dogany-skill-creator/SKILL.md
#       "user language ONLY where needed: trigger phrases, field / column
#        names, example utterances, user-facing output strings"
#       "description = match text ... list real trigger utterances
#        (user language)."
#       "display_name = short user-facing label"
# The skill-creator AGREES with the exemption; no conflict to report.
# `name` is included because it is an identifier line, never prose.
FRONTMATTER_EXEMPT_KEYS = ("description", "display_name", "name")

# EXEMPTION 3 -- quoted regions (fences, blockquotes, inline quotes).
# See module docstring, "QUOTED TEXT".

_FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:")

_INLINE_QUOTE_SUBS = (
    re.compile(r'"[^"\n]{0,300}"'),
    re.compile(r"'[^'\n]{0,300}'"),
    re.compile(r"`[^`\n]{0,300}`"),
    re.compile(r"[\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f]"
               r"[^\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\n]{0,300}"
               r"[\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f]"),
)

# Reason codes emitted for every exempted line, so the report can explain
# each removal instead of silently shrinking the number.
R_FENCE = "fenced-code-block"
R_BLOCKQUOTE = "blockquote"
R_INLINE_QUOTE = "inline-quote"
R_PERSONA_VALUE = "persona-data-value"
R_FRONTMATTER = "frontmatter-trigger-text"


_ESCAPED_QUOTE_RE = re.compile(r"\\[\"']")


def strip_inline_quotes(line):
    """Remove inline-quoted spans. A quoted instance-language phrase or a
    quoted date is a citation / example, not authored prose.

    Backslash-escaped quotes are dropped FIRST. Without that, a nested
    sample like `"<label> -- \\"<sample>\\""` mis-pairs: the opening quote
    binds to the escaped one and the sample text survives as a phantom
    finding. Real instance: dogany-user-onboarding/SKILL.md.

    KNOWN LIMITATION (not fixed here): a quoted span that OPENS on one line
    and CLOSES on the next is not stripped -- this is a line-local pass, and
    the report is line-anchored. A real instance exists in
    dogany-user-onboarding/SKILL.md.
    Documented rather than silently widened: a multi-line quote tracker would
    also swallow whole rule paragraphs after any unbalanced quote character.
    """
    line = _ESCAPED_QUOTE_RE.sub(" ", line)
    for pat in _INLINE_QUOTE_SUBS:
        line = pat.sub(" ", line)
    return line


# --- framework-scope filter (DGN-1333) --------------------------------------
# The WRITING MANDATE governs FRAMEWORK docs. A domain agent's own skills are
# product content owned by that agent's PO, and instance-language prose in
# them is by design -- measured 2026-09-08: 2396 findings on one domain
# instance vs 67 on another with an unscoped set. A 2396-item warning is not
# a warning, so an unscoped lint ships noise and dies unread. Scope test: the
# doc is framework-owned iff the SAME relative path also exists in the
# canonical template (the thing the update channel ships). Instance-only docs
# are counted and reported, never silently dropped.
TEMPLATE_ROOT = os.environ.get(
    "DOGANY_TEMPLATE_ROOT",
    os.path.expanduser("~/dogany/dogany-agent/agents/.template"),
)


def _template_available():
    return os.path.isdir(TEMPLATE_ROOT)


def partition_scope(rels):
    """Split resolved paths into (framework_owned, instance_only).

    Template missing -> everything is treated as in scope and the caller must
    say so. Fail-open on the SCOPE only; never fail-open silently.
    """
    if not _template_available():
        return list(rels), []
    inside, outside = [], []
    for rel in rels:
        (inside if os.path.isfile(os.path.join(TEMPLATE_ROOT, rel)) else outside).append(rel)
    return inside, outside


def scan_files(repo_root=REPO_ROOT):
    """Resolve SCAN_SET to existing repo-relative file paths, in order.

    isfile() is applied to glob hits too, not just to the literal entries:
    live instances symlink .claude/skills/<name> into a shared bundle, and a
    dangling link would otherwise reach open() and abort the run.
    """
    out = []
    for entry in SCAN_SET:
        if any(ch in entry for ch in "*?["):
            matches = sorted(glob.glob(os.path.join(repo_root, entry)))
            out.extend(os.path.relpath(m, repo_root) for m in matches
                       if os.path.isfile(m))
        else:
            if os.path.isfile(os.path.join(repo_root, entry)):
                out.append(entry)
    return out


def _persona_exempt(rel_path, line):
    for pat in PERSONA_VALUE_EXEMPT.get(rel_path, ()):
        if pat.match(line):
            return True
    return False


def analyse_text(text, rel_path):
    """Returns (findings, exemptions).

    findings   [{'check','line','text'}]      -- rule violations
    exemptions [{'check','line','text','reason'}] -- lines that WOULD have
               been findings on the raw text but were removed by an
               exemption. Reported so every removal is accountable.
    """
    findings = []
    exemptions = []
    in_fence = False
    in_frontmatter = False
    frontmatter_key = None

    lines = text.splitlines()
    for idx, raw in enumerate(lines, start=1):
        stripped_lead = raw.lstrip()

        # --- frontmatter tracking (only a leading '---' opens one) ---
        if raw.rstrip() == "---" and not in_fence:
            if idx == 1:
                in_frontmatter = True
                continue
            if in_frontmatter:
                in_frontmatter = False
                frontmatter_key = None
                continue

        has_hangul = bool(HANGUL_RE.search(raw))
        has_stamp = bool(DATE_RE.search(raw) and ATTRIBUTOR_RE.search(raw))

        if in_frontmatter:
            m = _FRONTMATTER_KEY_RE.match(raw)
            if m:
                frontmatter_key = m.group(1).lower()
            if frontmatter_key in FRONTMATTER_EXEMPT_KEYS:
                for check, hit in (("KOREAN_IN_PROSE", has_hangul),
                                   ("PROVENANCE_DATE", has_stamp)):
                    if hit:
                        exemptions.append({
                            "check": check, "line": idx, "text": raw,
                            "reason": "%s(%s)" % (R_FRONTMATTER,
                                                  frontmatter_key),
                        })
                continue
            # non-exempt frontmatter keys fall through to the normal checks

        # --- fenced code blocks ---
        if stripped_lead.startswith("```") or stripped_lead.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            for check, hit in (("KOREAN_IN_PROSE", has_hangul),
                               ("PROVENANCE_DATE", has_stamp)):
                if hit:
                    exemptions.append({"check": check, "line": idx,
                                       "text": raw, "reason": R_FENCE})
            continue

        # --- blockquotes ---
        if stripped_lead.startswith(">"):
            for check, hit in (("KOREAN_IN_PROSE", has_hangul),
                               ("PROVENANCE_DATE", has_stamp)):
                if hit:
                    exemptions.append({"check": check, "line": idx,
                                       "text": raw, "reason": R_BLOCKQUOTE})
            continue

        # --- the four persona data values ---
        if _persona_exempt(rel_path, raw):
            for check, hit in (("KOREAN_IN_PROSE", has_hangul),
                               ("PROVENANCE_DATE", has_stamp)):
                if hit:
                    exemptions.append({"check": check, "line": idx,
                                       "text": raw,
                                       "reason": R_PERSONA_VALUE})
            continue

        # --- inline quotes: strip, then judge what is left ---
        prose = strip_inline_quotes(raw)

        if has_hangul:
            if HANGUL_RE.search(prose):
                findings.append({"check": "KOREAN_IN_PROSE", "line": idx,
                                 "text": raw})
            else:
                exemptions.append({"check": "KOREAN_IN_PROSE", "line": idx,
                                   "text": raw, "reason": R_INLINE_QUOTE})

        if has_stamp:
            if DATE_RE.search(prose) and ATTRIBUTOR_RE.search(prose):
                findings.append({"check": "PROVENANCE_DATE", "line": idx,
                                 "text": raw})
            else:
                exemptions.append({"check": "PROVENANCE_DATE", "line": idx,
                                   "text": raw, "reason": R_INLINE_QUOTE})

    return findings, exemptions


def analyse_file(rel_path, repo_root=REPO_ROOT):
    path = os.path.join(repo_root, rel_path)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return analyse_text(fh.read(), rel_path)


def _clip(text, width):
    text = text.strip()
    return text if len(text) <= width else text[:width - 3] + "..."


def main(argv=None):
    parser = argparse.ArgumentParser(description="Baseline authoring lint "
                                                 "(report-only, always exit 0)")
    parser.add_argument("--repo-root", default=REPO_ROOT)
    parser.add_argument("--show-exemptions", action="store_true",
                        help="also print every line an exemption removed, "
                             "with its reason code")
    parser.add_argument("--quiet", action="store_true",
                        help="print only the SUMMARY line")
    parser.add_argument("--width", type=int, default=110)
    args = parser.parse_args(argv)

    all_files = scan_files(args.repo_root)
    files, out_of_scope = partition_scope(all_files)
    scope_note = ("framework-owned (template-matched)" if _template_available()
                  else "UNSCOPED: template not found at %s" % TEMPLATE_ROOT)
    tot_ko = tot_dt = 0
    raw_ko = raw_dt = 0
    clean_files = 0
    skipped = []

    for rel in files:
        try:
            findings, exemptions = analyse_file(rel, args.repo_root)
        except OSError as exc:
            # A file that vanished or turned unreadable between the scan and
            # the read is a SKIP with a reason, never an aborted run: this is
            # report-only tooling and a partial number that says so beats no
            # number at all.
            skipped.append((rel, exc.strerror or "unreadable"))
            continue
        ko = [f for f in findings if f["check"] == "KOREAN_IN_PROSE"]
        dt = [f for f in findings if f["check"] == "PROVENANCE_DATE"]
        ex_ko = [e for e in exemptions if e["check"] == "KOREAN_IN_PROSE"]
        ex_dt = [e for e in exemptions if e["check"] == "PROVENANCE_DATE"]
        tot_ko += len(ko)
        tot_dt += len(dt)
        raw_ko += len(ko) + len(ex_ko)
        raw_dt += len(dt) + len(ex_dt)
        if not ko and not dt:
            clean_files += 1
        if args.quiet:
            continue
        if ko or dt or (args.show_exemptions and (ex_ko or ex_dt)):
            print("%s  KOREAN_IN_PROSE=%d PROVENANCE_DATE=%d"
                  % (rel, len(ko), len(dt)))
            for f in ko + dt:
                print("    %-16s %s:%d: %s"
                      % (f["check"], rel, f["line"], _clip(f["text"],
                                                           args.width)))
            if args.show_exemptions:
                for e in ex_ko + ex_dt:
                    print("    exempt %-16s %s:%d [%s] %s"
                          % (e["check"], rel, e["line"], e["reason"],
                             _clip(e["text"], args.width)))

    if skipped and not args.quiet:
        for rel, why in skipped:
            print("SKIPPED %s (%s)" % (rel, why))

    if out_of_scope and not args.quiet:
        print("")
        print("out-of-scope (instance-owned, mandate does not apply): %d file(s)"
              % len(out_of_scope))
        for rel in out_of_scope:
            print("    skip %s" % rel)

    print("SUMMARY korean_in_prose=%d provenance_date=%d files=%d "
          "clean_files=%d raw_korean=%d raw_date=%d exempted_korean=%d "
          "exempted_date=%d skipped=%d out_of_scope=%d scope=%s"
          % (tot_ko, tot_dt, len(files), clean_files, raw_ko, raw_dt,
             raw_ko - tot_ko, raw_dt - tot_dt, len(skipped),
             len(out_of_scope), scope_note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
