#!/usr/bin/env python3
"""user-gate-lint.py -- DGN-1506 item 3: publish-gate lint for user-reachable
gate copy. ONE judge, three doors (DGN-1118 precedent -- never a second
implementation):

  framework door (release.sh gate 5be):
      user-gate-lint.py --root <repo>          # C0-C7
  pack door (pack_publish.sh gate (h)):
      user-gate-lint.py --payload <dir>        # C3/C5/C6 (+C0/C1/C2/C4/C7
                                               #  iff the payload ships
                                               #  gates.json)
  kit-text door (mint_run.sh handover-brief, RUNTIME):
      user-gate-lint.py --kit-text < text      # C3/C5/C6/C7 on stdin
      The two publish-time doors judge files this repo can see; a kit's
      handover brief is produced at MINT TIME by code the framework never
      read, so the only seat where the same judge can reach it is the
      moment before rendering. exit 1 = the text must NOT reach the owner
      (the caller degrades to "no screen", never to relayed findings).
      Content here is machine-relayed planned-class owner copy, so it is
      held to the gates.json user_hint_ko standard (C5/C6 + C3 honorifics
      + C7 fault vocabulary) plus the placeholder rules (neither {USER_LABEL}
      nor <오너 호칭> resolves on this lane) and the estate copy rules that
      until now existed only as SKILL.md prose: no em-dash (owner ruling
      2026-09-17, "콜론"), "담당 에이전트" never "비서".

Checks:
  C0  classification completeness (fail-closed): a gate the lint cannot
      classify is a gate it refuses. Every registry entry must declare
      class (planned|fault), user_reachable (JSON boolean), script,
      operator_reason and user_hint_ko (string; '' = copy pending). A
      missing/mistyped field is not an exemption -- it is a refusal. The
      registry's schema_version must be one this lint understands (1);
      an unknown version is refused, never guessed at.
  C1  hint completeness: every gates.json entry with user_reachable=true must
      carry a non-empty user_hint_ko. Owner ruling: a skill must never
      translate gate text -- needing a translation IS the defect. An empty
      hint on a user-reachable gate therefore BLOCKS publication (that is the
      forcing point, not a bug in this lint).
  C2  call-site cross-check: every planned+user_reachable gate id must appear
      as a `_gate_stop <id>` call site in its declared script, and every
      call site must name a registered id -- keeps the registry a machine
      fact instead of prose (doctrine leg (a)).
  C3  honorific hardcode: the finite owner-honorific set must not appear in
      user-copy carriers. The judgment seat is THIS gate, not a reviewer's
      attention (DGN-1506: the reviewer-attention rule was recognized as a
      self-violation of the (a)/(b) doctrine on the day it was written).
      Sanctioned spellings that pass: the machine-resolved template token
      {USER_LABEL}; the SKILL.md placeholder <오너 호칭> (model-lane copy).
      A literal honorific = the DGN-1464 shape (owner's address term
      inherited by strangers' agents).
      MATCH-CORPUS MARKER (DGN-1818): a regex / corpus literal whose job is
      to MATCH speech (e.g. third-party titles stripped from OTHER people's
      names) is not an owner address and is out of the address rule's
      scope. Such a line declares itself with a trailing comment on the
      SAME line:   <literal>  # user-gate-lint: match-corpus -- <reason>
      Narrow by construction: code carriers (.py/.sh) only, never copy
      (.md); exempts C3 on that one line only (C5/C6 still judge it); the
      line must carry a string literal; the reason is mandatory (a bare
      marker is itself a finding). Every honored marker is printed, so an
      exemption is never silent. No file-level exemption exists here.
  C4  reachability derived from code, not from the author: the ONLY machine
      path that carries a planned gate's id to the user-hint sender is the
      DOGANY_GATE_STATE_FILE marker channel (exported by the mint_run.sh
      pipeline transaction; _gate_stop writes it; the pipeline EXIT trap
      reads it). So the code-derived LOWER BOUND on user reachability is:
      every script transitively invocable from an exporter of that variable
      (invocation edges = non-comment references to sibling *.sh basenames;
      over-approximation errs toward MORE hint duty, i.e. fail-closed).
      A `_gate_stop` call site inside that derived set whose registry entry
      declares user_reachable=false is refused -- the declaration loses to
      the code. The reverse direction (proving NON-reachability) is not
      mechanically derivable from bash at file granularity; a declared-false
      entry OUTSIDE the derived set therefore stands as the recorded
      judgment C0 forces it to be. If no exporter exists at all while the
      registry declares user_reachable gates, the marker channel itself is
      gone and the lint refuses (fail-closed, not vacuous).
  C5  no English prose in owner-facing copy (DGN-1543 R2): a SKILL.md line
      the flow marks with `<!-- OWNER-SAY -->` ... `<!-- /OWNER-SAY -->`
      (the machine-readable version of the "internal only, never shown to
      user" prose that R2 measured going unread), and every gates.json
      user_hint_ko, must not carry >= OWNER_SAY_EN_THRESHOLD English words
      once identifiers are stripped (backtick `code`, @handles, <placeholder>
      and {UPPER_SNAKE} runtime slots, the `[[OPTIONS: ...]]` marker, and dotted tokens like
      t.me/handle or file.ext). This is why the threshold is a WORD COUNT,
      not a bare "no ASCII letters" rule: a lone @BotFather or `/newbot`
      never trips it, but a leaked English sentence -- which is what R2
      measured -- always does (DGN-1518's lesson: a predicate that cannot
      tell prose from an identifier blunts itself with false positives, so
      it must be measured, not assumed).
  C6  no internal gate/transaction vocabulary in owner-facing copy (DGN-1543
      R3): the same OWNER-SAY blocks and user_hint_ko strings must not carry
      any word from a small fixed internal-state vocabulary (게이트,
      트랜잭션, 마커, 무응답, ...) -- the species R3 measured: gate/marker
      state printed verbatim where the owner reads it. Scoped to the same
      carriers as C5, for the same reason.
  C7  planned gates do not wear fault vocabulary (DGN-1543 R4): the registry
      already classifies every gate -- class=planned is a normal branch (the
      flow is asking the owner a question), class=fault is something actually
      broken. The copy must READ that declaration: a class=planned entry's
      user_hint_ko must not carry any token of FAULT_VOCAB (a MEASURED
      separating set, see the constant). Measured defect (live owner run
      2026-09-17): four hints on planned gates all opened "⚠️ ... 멈췄어요"
      and the owner read a normal consent point as an accident, verbatim
      (17:43): "이런거였어? 프로세스인데 큰일나는거처럼 얘기하네". Same
      species as R2/R3: a declaration exists and nothing reads it. Scoped to
      gates.json ONLY -- it is the one carrier that carries the class
      declaration the copy is supposed to read; OWNER-SAY blocks declare no
      class, so C7 has nothing to hold them to.

An unterminated `<!-- OWNER-SAY -->` (opened but never closed) is refused by
C5/C6 as a structural finding -- fail-closed, the same doctrine as C0.

Exit 0 = clean; exit 1 = at least one finding (all findings printed, never
first-failure-stops). stdlib only, no third-party deps.
"""

import argparse
import json
import os
import re
import sys

# The finite owner-honorific set (DGN-1506 ticket, 검수 기준 정정 section).
# DOGANY_HONORIFIC_EXTRA (colon-separated) extends it with an instance's
# configured value where one exists at lint time.
HONORIFICS = ["형님", "오빠", "누나", "사장님"]

# Files whose honorific occurrences are DETECTION MACHINERY, not user copy.
# This allowlist is the recorded judgment; adding to it is a reviewed change.
ALLOW = {
    "scripts/pack/lib/relnote_authorship.sh":
        "honorific-detection regexes/canaries for the relnote authorship "
        "lint -- patterns about the words, not copy carrying them",
    "scripts/pack/lib/persona_prose.sh":
        "honorific word set + canaries for compat-lint C5 gate(c) persona "
        "prose (DGN-1526) -- the detector's vocabulary, not user copy",
}

# Carrier sets, framework door (repo-relative glob dirs; (kind, pattern)).
# kind 'code' skips comment lines (a comment cannot reach a user); kind
# 'copy' scans every line (SKILL.md is model-relayed copy end to end).
FRAMEWORK_CARRIERS = [
    ("code", "scripts/pack", ".sh"),   # recursive: lib/ included
    ("copy", "skills", "SKILL.md"),
    ("code", "skills", ".py"),
]

CALLSITE_RE = re.compile(r"""_gate_stop\s+["']?([A-Za-z0-9._-]+)["']?""")
COMMENT_RE = re.compile(r"^\s*#")
MATCH_CORPUS_RE = re.compile(r"#\s*user-gate-lint:\s*match-corpus\b(.*)$")
# file:line of every honored match-corpus marker (printed by main()).
C3_CORPUS_HONORED = []
EXPORT_RE = re.compile(r"export\s+DOGANY_GATE_STATE_FILE=")

# C0: the registry format this lint knows how to judge. Anything else is
# refused, not guessed at.
SCHEMA_VERSION = 1
VALID_CLASSES = ("planned", "fault")

# C5/C6: owner-facing copy marker (DGN-1543 R2/R3). A skill wraps the exact
# text it will say to the owner between these two lines; everything outside
# the markers -- prose explaining the template, "internal only" narration,
# forbidden-example screens -- is deliberately NOT scanned. Narrow on
# purpose: DGN-1518 measured what a carrier-blind predicate does to its own
# credibility (33/46 false positives on plain English prose).
OWNER_SAY_START_RE = re.compile(r"<!--\s*OWNER-SAY\s*-->")
OWNER_SAY_END_RE = re.compile(r"<!--\s*/OWNER-SAY\s*-->")

# identifiers that are allowed to carry ASCII letters inside owner-facing
# copy: backtick code spans, <placeholder> slots, @handles, the [[OPTIONS]]
# button marker, and dotted tokens (t.me/handle, a filename, a domain).
_C5_STRIP_RES = [
    re.compile(r"`[^`\n]*`"),
    re.compile(r"<[^>\n]+>"),
    re.compile(r"\{[A-Z][A-Z0-9_]*\}"),  # runtime-substituted {UPPER_SNAKE} slot (e.g. mint_run.sh {AGENT_DISPLAY_NAME})
    re.compile(r"@[A-Za-z0-9_]+"),
    re.compile(r"\[\[OPTIONS[^\]]*\]\]"),
    re.compile(r"\b[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?:/\S*)?"),
]
_EN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
OWNER_SAY_EN_THRESHOLD = 3  # >= this many stripped English words = prose

# C6 fixed vocabulary (DGN-1543 R3 + the FORBIDDEN list this flow's own
# SKILL.md already declared in prose at the step-6c crew-join branch --
# this check is that list's forcing point, not a new judgment).
INTERNAL_STATE_VOCAB = [
    "게이트", "트랜잭션", "TXN", "마커", "무응답", "막혔습니다",
    "안전장치", "거부", "스코프", "크루", "원장", "마이그레이션",
    "락스텝", "재시작 승인",
]

# C7 fault vocabulary (DGN-1543 R4). A MEASURED separating set, not a style
# guess -- derived from every current user_hint_ko of both classes at the
# time of writing: the fault-class copy (mint.fault) carries exactly these
# tokens; the planned-class copy written under the owner's rulings
# (mint.occupied, R6) carries none of them. These are the tokens the owner
# read as an accident on a normal consent screen (2026-09-17 17:43).
# Deliberately NOT in the set: the volitional 멈추겠습니다 (the mint-agent
# SKILL.md interim OWNER-SAY pause) -- "I will pause here" is a plan
# speaking, 멈췄어요 is an accident being reported; the lemma is shared, the
# voice is not, so the match is the perfective stem only. Widening this list
# is a reviewed change, and never a substitute for owner-approved copy.
FAULT_VOCAB = [
    "⚠",   # ⚠ warning sign (matches with or without the emoji selector)
    "멈췄",      # perfective "it stopped" -- accident voice
    "중단",      # abort/interruption
]

# kit-text door ONLY (base-kit handover-brief slot). These carry estate copy
# rules that until this door existed only as prose in mint-agent SKILL.md --
# scoped here so no existing carrier's verdict shifts. Tuples: (token, rule).
KIT_TEXT_FORBIDDEN = [
    ("—", "em-dash is forbidden in owner-facing copy; use a colon "
               "(owner ruling 2026-09-17, verbatim: 콜론)"),
    (" -- ", "the ASCII em-dash spelling is the same separator by another "
             "name; use a colon (owner ruling 2026-09-17)"),
    ("비서", "the estate says 담당 에이전트, never 비서"),
    ("{USER_LABEL}", "this lane never resolves the template token -- it "
                     "would reach the owner literally"),
    ("<오너 호칭>", "model-lane placeholder; this machine lane would show "
                    "it literally"),
]


def _c5_strip_carriers(text):
    for rx in _C5_STRIP_RES:
        text = rx.sub(" ", text)
    return text


def _iter_owner_say_blocks(path):
    """yield (start_line, end_line_or_None, text) per OWNER-SAY span.
    end_line is None for an opened-but-never-closed block (fail-closed)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return
    i, n = 0, len(lines)
    while i < n:
        if OWNER_SAY_START_RE.search(lines[i]):
            start = i + 2  # 1-based line number of the first body line
            j = i + 1
            body = []
            while j < n and not OWNER_SAY_END_RE.search(lines[j]):
                body.append(lines[j])
                j += 1
            if j >= n:
                yield (start, None, "".join(body))
                i = j
            else:
                yield (start, j + 1, "".join(body))
                i = j + 1
        else:
            i += 1


def _check_c5_c6_text(carrier, text, findings):
    stripped = _c5_strip_carriers(text)
    en_words = _EN_WORD_RE.findall(stripped)
    if len(en_words) >= OWNER_SAY_EN_THRESHOLD:
        findings.append(
            "C5 %s: owner-facing text carries %d English word(s) outside "
            "identifiers/@handles/`commands`/<placeholders> (e.g. %s) -- "
            "owner-facing copy in this flow must be Korean prose "
            "(DGN-1543 R2)" % (carrier, len(en_words), " ".join(en_words[:6])))
    for w in INTERNAL_STATE_VOCAB:
        if w in text:
            findings.append(
                "C6 %s: owner-facing text carries internal state "
                "vocabulary '%s' -- gate/transaction machinery must not "
                "narrate itself to the owner (DGN-1543 R3)" % (carrier, w))


def check_c5_c6_file(path, rel, findings):
    for start, end, text in _iter_owner_say_blocks(path):
        if end is None:
            findings.append(
                "C5 %s:%d: <!-- OWNER-SAY --> opened but never closed "
                "(missing <!-- /OWNER-SAY -->) -- fail-closed" % (rel, start))
            continue
        carrier = "%s:%d-%d" % (rel, start, end - 1)
        _check_c5_c6_text(carrier, text, findings)


def check_c5_c6_gates(gates_path, gates, findings):
    for gid, g in sorted(gates.items()):
        if not isinstance(g, dict):
            continue  # already refused by C0
        hint = g.get("user_hint_ko", "")
        if not isinstance(hint, str) or not hint.strip():
            continue  # empty hint already refused by C1
        _check_c5_c6_text("%s: gate '%s' user_hint_ko" % (gates_path, gid),
                          hint, findings)


def check_c7_gates(gates_path, gates, findings):
    for gid, g in sorted(gates.items()):
        if not isinstance(g, dict):
            continue  # already refused by C0
        if g.get("class") != "planned":
            continue
        hint = g.get("user_hint_ko", "")
        if not isinstance(hint, str) or not hint.strip():
            continue  # missing copy is C1's finding, not a voice finding
        for w in FAULT_VOCAB:
            if w in hint:
                findings.append(
                    "C7 %s: gate '%s' is class=planned (a normal branch: "
                    "the flow is asking the owner a question) but its "
                    "user_hint_ko wears fault vocabulary %r -- the owner "
                    "read exactly this voice as an accident (DGN-1543 R4, "
                    "2026-09-17 17:43 verbatim). The class declaration "
                    "exists so the copy can read it. Fix is owner-approved "
                    "planned-voiced copy in gates.json, or reclassifying "
                    "the gate if it truly is a fault -- never widening the "
                    "copy to pass" % (gates_path, gid, w))


def _honorific_res():
    words = list(HONORIFICS)
    extra = os.environ.get("DOGANY_HONORIFIC_EXTRA", "")
    for w in extra.split(":"):
        w = w.strip()
        if w:
            words.append(w)
    return words


def _iter_files(base, sub, suffix):
    root = os.path.join(base, sub)
    if not os.path.isdir(root):
        return
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            if suffix.startswith("."):
                if not f.endswith(suffix):
                    continue
            elif f != suffix:
                continue
            yield os.path.join(dirpath, f)


def _load_gates(path, findings):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:  # unreadable registry = fail-closed, loudly
        findings.append("%s: gates.json unreadable/invalid (%s) -- "
                        "fail-closed" % (path, e))
        return None
    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        findings.append(
            "C0 %s: schema_version %r is not %d -- a registry this lint "
            "cannot read by its own rules is refused, not judged by "
            "guesswork (fail-closed)" % (path, sv, SCHEMA_VERSION))
        return None
    gates = data.get("gates")
    if not isinstance(gates, dict):
        findings.append("%s: no 'gates' object -- fail-closed" % path)
        return None
    return gates


def check_c0(gates_path, gates, findings):
    for gid, g in sorted(gates.items()):
        if not isinstance(g, dict):
            findings.append(
                "C0 %s: gate '%s' is not an object -- cannot classify, "
                "refused (fail-closed)" % (gates_path, gid))
            continue
        if g.get("class") not in VALID_CLASSES:
            findings.append(
                "C0 %s: gate '%s' class=%r is not one of %s -- a gate the "
                "lint cannot classify is a gate it refuses"
                % (gates_path, gid, g.get("class"), "|".join(VALID_CLASSES)))
        if not isinstance(g.get("user_reachable"), bool):
            findings.append(
                "C0 %s: gate '%s' user_reachable=%r is not a JSON boolean "
                "-- reachability must be a declared true/false the checks "
                "can stand on; missing/other = unclassified = refused"
                % (gates_path, gid, g.get("user_reachable")))
        if not isinstance(g.get("script"), str) or not g.get("script").strip():
            findings.append(
                "C0 %s: gate '%s' has no 'script' -- the declaration must "
                "name where the gate lives (fail-closed)" % (gates_path, gid))
        if not isinstance(g.get("user_hint_ko"), str):
            findings.append(
                "C0 %s: gate '%s' user_hint_ko must be a string ('' = copy "
                "pending); got %r -- refused"
                % (gates_path, gid, g.get("user_hint_ko")))
        if not isinstance(g.get("operator_reason"), str) \
                or not g.get("operator_reason").strip():
            findings.append(
                "C0 %s: gate '%s' has no operator_reason -- two audiences, "
                "two fields, ONE declaration site; the operator summary is "
                "part of the declaration (fail-closed)" % (gates_path, gid))


def check_c1(gates_path, gates, findings):
    for gid, g in sorted(gates.items()):
        if not isinstance(g, dict):
            continue  # already refused by C0
        if not g.get("user_reachable"):
            continue
        hint = g.get("user_hint_ko", "")
        if not isinstance(hint, str) or not hint.strip():
            findings.append(
                "C1 %s: gate '%s' is user-reachable but user_hint_ko is "
                "empty -- the sentence the user will read does not exist; "
                "publication blocked until owner-approved copy lands "
                "(DGN-1506 item 3)" % (gates_path, gid))
        elif "<오너 호칭>" in hint:
            findings.append(
                "C1 %s: gate '%s' user_hint_ko carries the model-lane "
                "placeholder <오너 호칭> -- machine-sent copy resolves "
                "{USER_LABEL} only; this would reach the user literally"
                % (gates_path, gid))


def check_c2(base, gates_path, gates, findings):
    scripts_dir = os.path.dirname(gates_path)
    # forward: declared planned user-reachable gate -> call site must exist
    for gid, g in sorted(gates.items()):
        if not isinstance(g, dict):
            continue  # already refused by C0
        if g.get("class") != "planned" or not g.get("user_reachable"):
            continue
        script = os.path.join(scripts_dir, g.get("script", ""))
        sites = _callsites(script)
        if gid not in sites:
            findings.append(
                "C2 %s: gate '%s' declared in %s but no `_gate_stop %s` "
                "call site found there -- registry drifted from code"
                % (gates_path, gid, g.get("script", "?"), gid))
    # reverse: every call site must be registered
    seen_scripts = set()
    for sub in ("", "lib"):
        d = os.path.join(scripts_dir, sub)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if f.endswith(".sh") and os.path.isfile(p) and p not in seen_scripts:
                seen_scripts.add(p)
                for gid, line_no in _callsites(p, with_lines=True):
                    if gid not in gates:
                        findings.append(
                            "C2 %s:%d: `_gate_stop %s` call site names an "
                            "id absent from gates.json -- register it (with "
                            "its user_hint_ko) or use _fail"
                            % (p, line_no, gid))


def _callsites(script_path, with_lines=False):
    out = [] if with_lines else set()
    if not os.path.isfile(script_path):
        return out
    with open(script_path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if COMMENT_RE.match(line):
                continue
            if "_gate_stop()" in line:  # the function definition itself
                continue
            m = CALLSITE_RE.search(line)
            if not m:
                continue
            if with_lines:
                out.append((m.group(1), i))
            else:
                out.add(m.group(1))
    return out


def _noncomment_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not COMMENT_RE.match(line):
                    yield line
    except OSError:
        return


def check_c4(scan_root, gates_path, gates, findings):
    """Reachability derived from code (docstring C4). scan_root is the
    directory whose *.sh files form the invocation-graph domain (framework
    door: scripts/pack; payload door: the payload root)."""
    files = []
    for dirpath, _dirs, names in os.walk(scan_root):
        for f in sorted(names):
            if f.endswith(".sh"):
                files.append(os.path.join(dirpath, f))
    exporters = [f for f in files
                 if any(EXPORT_RE.search(l) for l in _noncomment_lines(f))]
    declares_reachable = any(isinstance(g, dict)
                             and g.get("user_reachable") is True
                             for g in gates.values())
    if not exporters:
        if declares_reachable:
            findings.append(
                "C4 %s: registry declares user_reachable gates but NO "
                "script under %s exports DOGANY_GATE_STATE_FILE -- the "
                "marker channel that carries a fired gate's id to the "
                "hint sender does not exist in the code, so no declared "
                "reachability can be true; refused (fail-closed, not "
                "vacuous)" % (gates_path, scan_root))
        return
    by_name = {}
    for f in files:
        by_name.setdefault(os.path.basename(f), []).append(f)
    # one alternation, longest name first so full basenames win; the
    # lookbehind keeps `pack_install.sh` from feeding an `install.sh` edge
    ref_re = re.compile(
        r"(?<![A-Za-z0-9_-])("
        + "|".join(re.escape(n) for n in
                   sorted(by_name, key=len, reverse=True))
        + r")")
    adj = {f: set() for f in files}
    for f in files:
        for line in _noncomment_lines(f):
            for m in ref_re.finditer(line):
                adj[f].update(by_name[m.group(1)])
    reachable = set(exporters)
    stack = list(exporters)
    while stack:
        for nxt in adj[stack.pop()]:
            if nxt not in reachable:
                reachable.add(nxt)
                stack.append(nxt)
    anchor = ", ".join(os.path.relpath(e, scan_root) for e in exporters)
    for f in sorted(reachable):
        for gid in sorted(_callsites(f)):
            g = gates.get(gid)
            if not isinstance(g, dict):
                continue  # unregistered id: C2 reverse already refuses it
            if g.get("user_reachable") is not True:
                findings.append(
                    "C4 %s: `_gate_stop %s` fires in %s, which the code "
                    "reaches from the pipeline transaction (export "
                    "DOGANY_GATE_STATE_FILE in: %s) -- but the registry "
                    "declares user_reachable=%r. Reachability is decided "
                    "by the code, not by the author; the declaration is "
                    "refused. Flip it to true and carry a user_hint_ko, "
                    "or take the call site out of the transaction's reach"
                    % (gates_path, gid, os.path.relpath(f, scan_root),
                       anchor, g.get("user_reachable")))


def check_c3_file(path, rel, kind, words, findings):
    try:
        with open(path, encoding="utf-8", errors="strict") as fh:
            lines = fh.readlines()
    except (UnicodeDecodeError, OSError):
        return  # binary / unreadable: not a text copy carrier
    for i, line in enumerate(lines, 1):
        if kind == "code" and COMMENT_RE.match(line):
            continue
        mc = MATCH_CORPUS_RE.search(line) if kind == "code" else None
        if mc:
            reason = mc.group(1).strip().lstrip("-").strip()
            literal = line[:mc.start()]
            if len(reason) < 8:
                findings.append(
                    "C3 %s:%d: match-corpus marker without a reason -- "
                    "write `# user-gate-lint: match-corpus -- <why this "
                    "literal matches speech, not addresses the owner>`"
                    % (rel, i))
            elif '"' not in literal and "'" not in literal:
                findings.append(
                    "C3 %s:%d: match-corpus marker on a line with no "
                    "string literal -- the marker exempts the literal on "
                    "its own line only" % (rel, i))
            elif any(w in literal for w in words):
                C3_CORPUS_HONORED.append("%s:%d (%s)" % (rel, i, reason))
            continue
        for w in words:
            if w in line:
                findings.append(
                    "C3 %s:%d: hard-coded owner honorific '%s' in a "
                    "user-copy carrier -- use the machine-resolved "
                    "{USER_LABEL} (script copy) or <오너 호칭> (model-lane "
                    "SKILL.md copy); DGN-1464 shape" % (rel, i, w))
                break  # one finding per line is enough


def check_c3_gates(gates_path, gates, words, findings):
    for gid, g in sorted(gates.items()):
        if not isinstance(g, dict):
            continue  # already refused by C0
        hint = g.get("user_hint_ko", "")
        if not isinstance(hint, str):
            continue
        for w in words:
            if w in hint:
                findings.append(
                    "C3 %s: gate '%s' user_hint_ko hard-codes honorific "
                    "'%s' -- use {USER_LABEL}" % (gates_path, gid, w))


def run_framework(root):
    findings = []
    words = _honorific_res()
    gates_path = os.path.join(root, "scripts", "pack", "gates.json")
    if not os.path.isfile(gates_path):
        findings.append("%s: missing -- the user-gate registry is mandatory "
                        "machinery (fail-closed)" % gates_path)
    else:
        gates = _load_gates(gates_path, findings)
        if gates is not None:
            check_c0(gates_path, gates, findings)
            check_c1(gates_path, gates, findings)
            check_c2(root, gates_path, gates, findings)
            check_c4(os.path.join(root, "scripts", "pack"),
                     gates_path, gates, findings)
            check_c3_gates(gates_path, gates, words, findings)
            check_c5_c6_gates(gates_path, gates, findings)
            check_c7_gates(gates_path, gates, findings)
    for kind, sub, suffix in FRAMEWORK_CARRIERS:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for path in _iter_files(root, sub, suffix):
            rel = os.path.relpath(path, root)
            if rel in ALLOW:
                continue
            check_c3_file(path, rel, kind, words, findings)
            check_c5_c6_file(path, rel, findings)
    return findings


def run_payload(payload):
    findings = []
    words = _honorific_res()
    gates_path = os.path.join(payload, "gates.json")
    if os.path.isfile(gates_path):
        gates = _load_gates(gates_path, findings)
        if gates is not None:
            check_c0(gates_path, gates, findings)
            check_c1(gates_path, gates, findings)
            check_c2(payload, gates_path, gates, findings)
            check_c4(payload, gates_path, gates, findings)
            check_c3_gates(gates_path, gates, words, findings)
            check_c5_c6_gates(gates_path, gates, findings)
            check_c7_gates(gates_path, gates, findings)
    for dirpath, _dirs, files in os.walk(payload):
        for f in sorted(files):
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, payload)
            if f.endswith((".sh", ".py")):
                check_c3_file(path, rel, "code", words, findings)
                check_c5_c6_file(path, rel, findings)
            elif f.endswith(".md"):
                check_c3_file(path, rel, "copy", words, findings)
                check_c5_c6_file(path, rel, findings)
    return findings


def run_kit_text():
    """kit-text door: judge ONE piece of owner-facing text on stdin (the
    base-kit handover brief a kit CLI returned at mint time). Machine-relayed
    planned-class copy -> the gates.json user_hint_ko standard applies:
    C3 honorifics, C5 English prose, C6 internal vocabulary, C7 fault
    vocabulary, plus the KIT_TEXT_FORBIDDEN estate copy rules. Empty input is
    a CALLER error (absence must be decided before calling the judge -- a
    kit with nothing to say produces no screen, not an empty judgment)."""
    findings = []
    text = sys.stdin.read()
    carrier = "<kit-text>"
    if not text.strip():
        findings.append(
            "kit-text %s: empty input -- absence is the caller's branch "
            "(no screen), never a judgment call; refusing to bless "
            "emptiness (fail-closed)" % carrier)
        return findings
    _check_c5_c6_text(carrier, text, findings)
    for w in _honorific_res():
        if w in text:
            findings.append(
                "C3 %s: hard-coded owner honorific '%s' in kit-provided "
                "owner copy -- a kit cannot know this owner's address term "
                "(DGN-1464 shape)" % (carrier, w))
    for w in FAULT_VOCAB:
        if w in text:
            findings.append(
                "C7 %s: kit-provided handover copy wears fault vocabulary "
                "%r -- the handover brief is a planned branch (a question, "
                "not an accident report; DGN-1543 R4)" % (carrier, w))
    for tok, rule in KIT_TEXT_FORBIDDEN:
        if tok in text:
            findings.append(
                "kit-text %s: forbidden token %r -- %s" % (carrier, tok, rule))
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--root", help="framework repo root (release.sh door)")
    g.add_argument("--payload", help="pack payload root (pack_publish.sh door)")
    g.add_argument("--kit-text", action="store_true",
                   help="judge owner-facing kit text on stdin "
                        "(mint_run.sh handover-brief door)")
    args = ap.parse_args()
    if args.root:
        findings = run_framework(os.path.abspath(args.root))
        door = "framework"
    elif args.kit_text:
        findings = run_kit_text()
        door = "kit-text"
    else:
        findings = run_payload(os.path.abspath(args.payload))
        door = "payload"
    if findings:
        print("[user-gate-lint] %s door: %d finding(s)"
              % (door, len(findings)), file=sys.stderr)
        for f in findings:
            print("  XX " + f, file=sys.stderr)
        sys.exit(1)
    for h in C3_CORPUS_HONORED:
        print("[user-gate-lint] C3 match-corpus literal honored: %s" % h)
    print("[user-gate-lint] %s door: clean (C0-C7)" % door)
    sys.exit(0)


if __name__ == "__main__":
    main()
