#!/usr/bin/env python3
"""Decision ledger writer + confirmer (DGN-1952).

WHY THIS EXISTS: the persona rule "a user question -> ledger line + per-
decision file in the same edit" was followed in ~10% of decision menus (v2.7
D2 measurement: 930 of 1,033 menus left no ledger entry).  A rule that is not
followed gets a forcing point (rules/hot.framework.md Principles): the
machine requires a classified ledger entry before the menu leaves.

Hook seats, one script:
  pre   (PreToolUse hook) validate every new manual ledger entry before
        Write/Edit/MultiEdit. Opaque Bash/NotebookEdit writes are refused.
  stop  (Stop hook)  the final reply of an owner-session turn carries a real
        [[OPTIONS]] menu (lib/dispatch_return_forcing.has_options_marker --
        fence-aware, bridge-identical) and the turn wrote nothing to the
        ledger itself -> refuse with instructions to write Class/Not-How
        in the ledger. Each menu needs a distinct classified entry written
        or edited this turn, validated from persisted ledger/detail text.
        Machine PENDING capture and its same-turn pointer remain supported;
        the writer requires the same class fields. A turn that ends with no
        menu clears the pending pointer: the menu is no longer the
        immediately previous assistant turn.
  ups   (UserPromptSubmit hook)  the pending pointer names an entry written
        by the immediately previous assistant turn of THIS session and the
        user's message answers it (a button label, "N. <that label>", a
        number handle, or a bare number in range: decision_ledger_fmt.pick)
        -> mark that entry CONFIRMED with the user's words verbatim and the
        time.  Anything else leaves it pending.
  send  (bridge, at send time)  Rework A: the bridge hands the reply it is
        about to deliver; a real [[OPTIONS]] menu gets exactly one pending
        entry + the pointer, with NO agent obligation: Class/Not-How/Kind
        come from the reply when it carries them, else from BRIDGE_CLASS.
        The same reply sent twice reuses its entry. (The old `stop` seat is
        no longer wired anywhere; run_stop stays only as the dormant writer.)

SEAT CHOICE: a standalone script, not a branch of onboarding-gate-stop.py.
That seat owns onboarding + dispatch-return forcing. This one writes the
ledger and blocks unclassified owner menus (DGN-2012); it composes in the
Stop list and stays removable on its own.  It reuses that module's owner-session predicate
(session_is_interactive), its genuine-user-boundary rule and its log-dir
resolution instead of re-deriving them.

Owner session only: the live bridge session (session_is_interactive), never a
dispatched worker (DOGANY_DISPATCH_WORKER), a subagent (payload agent_id /
SubagentStop) or a cron / headless `claude -p` run.

Config (config/agent.conf; DECISION_LEDGER_PATH unset = whole hook is a
no-op, the template default):
  DECISION_LEDGER_PATH              ledger file (relative = instance root)
  DECISION_LEDGER_DIR               per-decision file dir (unset = no file)
  DECISION_LEDGER_ID_FORMAT         id pattern, "{n}" = the number
                                    (default "dec-{n}")
  DECISION_LEDGER_HEADING           "{id} {question} {date} {tag}"
  DECISION_LEDGER_PENDING_STATUS    "{date} {time}"
  DECISION_LEDGER_CONFIRMED_STATUS  "{date} {time}"
  DECISION_LEDGER_PENDING_TAG       "{status}"
  DECISION_LEDGER_CONFIRMED_TAG     "{status} {answer}"
The entry format (templates, mark, options line, pick, inbound reader,
clock) lives in lib/decision_ledger_fmt.py, shared with the Why net seed
and dec join.
Every machine entry carries one HTML comment line
"<!-- decision-ledger id=<id> state=pending|confirmed -->" under its heading:
the confirmer finds the entry by it, whatever the heading format is.
v2.7 Why net AMEND-1 6.2: a machine confirm also writes the request id of
the answering owner turn, "state=confirmed req=<request_id>", taken from the
same bridge inbound-context the why-net `ups` hook reads (fresh, and its
text_sha names THIS prompt).  An entry the agent wrote itself never carries
req=, so it never counts as an owner decision (Why net A2 / the dec join).

Concurrency: every ledger mutation runs under an exclusive flock on
"<ledger>.lock"; the id is allocated from the max id in the ledger AND the
per-decision dir under that lock, the per-decision file is created O_EXCL,
the append is one O_APPEND write, and a confirm rewrites via tmp + replace.

Restart reader, not a hook (DGN-1420):
  handover  the restart worker (bridge/self_restart.sh) prints the latest
        DECISION_HANDOVER_COUNT entries (default 10) into the post-restart
        spool, so a confirmed decision survives the cut conversation.

Capture storage errors remain fail-open: a SKIP line in
<BOT_DATA_DIR>/logs/decision-ledger.log and exit 0. DGN-2012 class refusals emit a blocking hook response before
recording or sending an owner question. PreToolUse checks manual raises too.

F13 seam (DGN-2025), public for devkit -- the names an external consumer
(the devkit dec verbs) imports, fixed by tests/dgn2025_f13_seam_selftest.py:
  this module     next_number, LedgerLock, confirm, record_for, conf, fmt
  decision_ledger_fmt  mark, MARK_RE, options_text, DEFAULTS, id_regex
record_for(id) restores a ledger record (tag included) for confirm().
Capture (run_stop -> write_pending, run_ups -> confirm) is unchanged.

Pure stdlib.  English/ASCII only.
"""
import datetime
import fcntl
import glob
import importlib.util
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CODE_ROOT = os.path.dirname(SCRIPT_DIR)
# Instance root (config/agent.conf, relative ledger paths, the data dir).
# DECISION_LEDGER_ROOT is a fixture override; live runs use the code root.
ROOT = os.environ.get("DECISION_LEDGER_ROOT") or CODE_ROOT
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
import decision_ledger_fmt as fmt  # noqa: E402  (DGN-1967 step 5)

LOG_FILENAME = "decision-ledger.log"
POINTER_FILENAME = ".decision-ledger-pending.json"
DISPATCH_ENV_MARKER = "DOGANY_DISPATCH_WORKER"
INJECTED_TURN_MARK = "[bridge:injected-turn]"

DEFAULTS = fmt.DEFAULTS
QUESTION_MAX = fmt.QUESTION_MAX
ANSWER_MAX = fmt.ANSWER_MAX
_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_PATH_KEYS = ("file_path", "notebook_path", "path")
# Mirrors bridge/options.py _OPTION_RE / _FENCED_CODE_RE (numbered option
# line, fenced-code exclusion).
_OPTION_RE = re.compile(r"^\s*(\d+)[.\u3001)\uff09]\s*(.+)")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)


def _load(filename, modname, base=SCRIPT_DIR):
    try:
        spec = importlib.util.spec_from_file_location(
            modname, os.path.join(base, filename))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_GATE = _load("onboarding-gate-stop.py", "_dl_onboarding_gate")
_DRF = _load(os.path.join("lib", "dispatch_return_forcing.py"), "_dl_drf")
# The one outbound-context strip (bridge/outbound_context.owner_region);
# None without the bridge, which then never writes a block.
_OC = _load(os.path.join("bridge", "outbound_context.py"), "_dl_outbound_context",
            os.path.dirname(SCRIPT_DIR))


# ---------------------------------------------------------------------------
# Config + logging
# ---------------------------------------------------------------------------

def conf(key):
    try:
        from conf_reader import conf_get
        value = conf_get(key, os.path.join(ROOT, "config", "agent.conf"))
    except Exception:
        value = ""
    return value or DEFAULTS.get(key, "")


def _abs(path):
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def ledger_paths():
    """(ledger, per-decision dir or None), or None when the hook is off."""
    ledger = conf("DECISION_LEDGER_PATH")
    if not ledger:
        return None
    dec_dir = conf("DECISION_LEDGER_DIR")
    return _abs(ledger), (_abs(dec_dir) if dec_dir else None)


def _data_dir():
    try:
        return os.path.dirname(_GATE._resolve_log_dir(ROOT))
    except Exception:
        return os.path.join(ROOT, ".telegram_bot")


def log(event, detail=""):
    try:
        log_dir = os.path.join(_data_dir(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, LOG_FILENAME), "a",
                  encoding="utf-8") as fh:
            fh.write("%s  %s  %s\n" % (
                datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                event, detail))
    except Exception:
        pass


def now():
    """The instance wall clock: lib/tz_offset.sh resolves the offset
    (agent.conf AGENT_TZ_OFFSET_HOURS, else the crew TZ_OFFSET_HOURS, else
    the host), the one resolver (DGN-1967 bug 20)."""
    return fmt.now(ROOT)


def stamp_fields(moment):
    return {"date": moment.strftime("%Y-%m-%d"),
            "time": moment.strftime("%H:%M")}


# ---------------------------------------------------------------------------
# Owner-session guard
# ---------------------------------------------------------------------------

def is_owner_session(data):
    if os.environ.get(DISPATCH_ENV_MARKER):
        return False
    if data.get("agent_id") or data.get("hook_event_name") == "SubagentStop":
        return False
    try:
        return bool(_GATE.session_is_interactive())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Menu parsing
# ---------------------------------------------------------------------------

def _unfenced_lines(text):
    spans = [m.span() for m in _FENCED_CODE_RE.finditer(text)]
    out, offset = [], 0
    for line in text.split("\n"):
        start, end = offset, offset + len(line)
        offset = end + 1
        out.append((line, any(s <= start and end <= e for s, e in spans)))
    return out


def _is_marker(line):
    try:
        return _DRF.is_options_marker_line(line)
    except Exception:
        return False


def _i18n_values(key):
    """`key` from every bridge i18n catalog (bridge/i18n/<lang>.py)."""
    values = []
    for path in sorted(glob.glob(os.path.join(CODE_ROOT, "bridge", "i18n",
                                              "*.py"))):
        name = os.path.basename(path)
        mod = _load(name, "_dl_i18n_" + name[:-3], os.path.dirname(path))
        value = (getattr(mod, "STRINGS", {}) or {}).get(key) if mod else None
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _rec_markers():
    """Every locale's recommendation marker (option_rec_marker)."""
    return _i18n_values("option_rec_marker") or ["(rec)"]


def _clean(text):
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s*(?:#+|>|[-*])\s*", "", text)
    return " ".join(text.split())


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


# DGN-2012: declarations are agent-authored, not a semantic classifier.
# A false What label still requires review; never infer class from keywords.
CLASS_HELP = ("decide it yourself: owner decisions require Class: Why or "
              "Class: What and Not-How: <one line explaining the owner outcome "
              "or purpose at stake>, and Kind: shape|scope|fork (aliases: "
              "\ubaa8\uc591|\ubc94\uc704|\uac08\ub9bc\uae38). How and missing declarations are refused. "
              "Before sending: include material for that kind; check that the "
              "user can tell the options apart from this text alone; cite any "
              "prior answer to the same question with its date.")
# DGN-2012 introduced the class gate on this date (commit a86637e4).
# Date-only historical headings must be strictly earlier to qualify.
CLASS_RULE_DATE = datetime.date(2026, 10, 12)
KIND_RULE_DATE = datetime.date(2026, 10, 29)


class ClassRefused(ValueError):
    pass


def class_fields(text, allow_legacy=False):
    fields = {"class": [], "not_how": [], "kind": []}
    for line, fenced in _unfenced_lines(text):
        if fenced:
            continue
        m = re.fullmatch(r"\s*(?:- )?(Class|Not-How|Kind):\s*(.*?)\s*", line)
        if m:
            key = {"Class": "class", "Not-How": "not_how", "Kind": "kind"}[m[1]]
            fields[key].append(m[2])
    if allow_legacy and fields == {"class": ["legacy"], "not_how": [], "kind": []}:
        return {"class": "legacy", "not_how": "", "kind": ""}
    historical = False
    dates = re.findall(r"\((\d{4}-\d{2}-\d{2})\)", text)
    if dates:
        try:
            historical = datetime.date.fromisoformat(dates[0]) < KIND_RULE_DATE
        except ValueError:
            pass
    if (len(fields["class"]) != 1 or len(fields["not_how"]) != 1 or
            (len(fields["kind"]) != 1 and not (historical and not fields["kind"]))):
        raise ClassRefused(CLASS_HELP)
    values = {k: (v[0] if v else "") for k, v in fields.items()}
    return validate_class(values, allow_missing_kind=historical)


def validate_class(fields, allow_missing_kind=False):
    reason = fields.get("not_how")
    kind = fmt.decision_kind(fields.get("kind"))
    if (fields.get("class") not in ("Why", "What") or
            not isinstance(reason, str) or not reason.strip() or
            len(reason.splitlines()) != 1 or
            reason.strip().lower() in ("n/a", "none", "tbd", "-", "not how") or
            (not kind and not allow_missing_kind)):
        raise ClassRefused(CLASS_HELP)
    return {"class": fields["class"], "not_how": reason.strip(),
            "kind": kind or ""}


MENU_LEDGER_HELP = (
    "Write or edit a decision ledger entry this turn for each menu, with "
    "Class: Why or Class: What and Not-How: <one line explaining the owner "
    "outcome or purpose at stake>, plus Kind: shape|scope|fork (or "
    "\ubaa8\uc591|\ubc94\uc704|\uac08\ub9bc\uae38). Keep these fields in the ledger; decide it "
    "yourself for How decisions. Include the declared kind's material, check "
    "that the user can tell the options apart from this text alone, and cite "
    "a prior answer to the same question with its date when one exists.")


def menu_classes(text, tools, ledger, dec_dir, cwd, pending_id=None):
    """Validate persisted entries touched THIS turn, never reply declarations.

    Reverse inspectable edits from the current file to identify changed
    entries, including edits to a body without its heading. Full Write calls
    touch every entry; unclassified historical headings remain history.
    Ledger/detail copies and repeated edits count once per decision id.
    """
    count = sum(not fenced and _is_marker(line)
                for line, fenced in _unfenced_lines(text))
    paths = {}
    for tool in tools:
        if not turn_wrote_ledger([tool], ledger, dec_dir, cwd):
            continue
        name, args = tool.get("name"), tool.get("input") or {}
        if name not in ("Write", "Edit", "MultiEdit"):
            raise ClassRefused(MENU_LEDGER_HELP)
        raw = next((args[k] for k in _PATH_KEYS if args.get(k)), "")
        path = os.path.realpath(raw if os.path.isabs(raw)
                                else os.path.join(cwd, raw))
        paths.setdefault(path, []).append((name, args))

    if pending_id:
        paths.setdefault(os.path.realpath(ledger), [])
    declarations = {}
    for path, operations in paths.items():
        try:
            with open(path, encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            raise ClassRefused(MENU_LEDGER_HELP + " Cannot read the ledger entry.")
        detail = bool(dec_dir and _same_or_under(path, dec_dir, True)
                      and not _same_or_under(path, ledger, False))
        ident = os.path.splitext(os.path.basename(path))[0]

        def entries(body):
            blocks = decision_blocks(body)
            if not blocks and detail and id_regex(
                    conf("DECISION_LEDGER_ID_FORMAT")).fullmatch(ident):
                return [(ident, body)]
            return blocks

        touched = {pending_id} if pending_id and _same_or_under(
            path, ledger, False) else set()
        after = current
        for name, args in reversed(operations):
            if name == "Write":
                if after != args.get("content", ""):
                    raise ClassRefused(MENU_LEDGER_HELP + " Cannot verify the ledger write.")
                for key, body in entries(after):
                    # A full ledger rewrite may carry pre-rule history.
                    if (not detail and not re.search(r"(?m)^\s*(?:- )?Class:", body)
                            and legacy_detail_id(ledger, os.path.join(
                                dec_dir or os.path.dirname(ledger), key + ".md"))):
                        continue
                    touched.add(key)
                break  # Write replaced the file; earlier changes are superseded.
            edits = args.get("edits", []) if name == "MultiEdit" else [args]
            for edit in reversed(edits):
                old, new = edit.get("old_string", ""), edit.get("new_string", "")
                if not old or not new or new not in after:
                    raise ClassRefused(MENU_LEDGER_HELP + " Cannot verify the ledger edit.")
                if not edit.get("replace_all") and after.count(new) != 1:
                    raise ClassRefused(MENU_LEDGER_HELP + " Use an unambiguous ledger edit.")
                before = after.replace(new, old, -1 if edit.get("replace_all") else 1)
                previous = dict(entries(before))
                touched.update(key for key, body in entries(after)
                               if previous.get(key) != body)
                after = before
        for key, body in entries(current):
            if key in touched:
                try:
                    declarations[key] = class_fields(body)
                except ClassRefused:
                    raise ClassRefused(MENU_LEDGER_HELP)
    if len(declarations) < count:
        raise ClassRefused(MENU_LEDGER_HELP + " Found %d ledger entries for %d menus."
                           % (len(declarations), count))
    return list(declarations.values())


def refuse(mode, reason=CLASS_HELP):
    if mode == "stop":
        clear_pointer()
    log("REFUSED_CLASS", "mode=" + mode)
    if mode == "pre":
        response = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                    "permissionDecision": "deny", "permissionDecisionReason": reason}}
    else:
        response = {"decision": "block", "reason": reason}
    print(json.dumps(response))


def parse_menu(text):
    """{question, labels, recommended} of the LAST real marker in `text`."""
    lines = _unfenced_lines(text)
    marker_idx, labels = None, []
    for i, (line, fenced) in enumerate(lines):
        if not fenced and _is_marker(line):
            marker_idx = i
    if marker_idx is None:
        return None
    stripped = lines[marker_idx][0].strip()
    if stripped.startswith("[[OPTIONS:"):
        labels = [p.strip() for p in stripped[len("[[OPTIONS:"):-2].split("|")
                  if p.strip()]
    if not labels:
        for line, fenced in lines[marker_idx + 1:]:
            if fenced or not line.strip() or _is_marker(line):
                break
            m = _OPTION_RE.match(line)
            labels.append(m.group(2).strip() if m else line.strip())
    # Body numbered run (the LAST contiguous 1..N run before the marker).
    run, cur = [], []
    for idx, (line, fenced) in enumerate(lines[:marker_idx]):
        m = None if fenced else _OPTION_RE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        if n == 1:
            cur = [(idx, m.group(2).strip())]
        elif cur and n == len(cur) + 1:
            cur.append((idx, m.group(2).strip()))
        else:
            continue
        run = cur
    if not labels:
        labels = [label for _, label in run]
    marks = _rec_markers()
    recommended = ""
    for pos, (_, body) in enumerate(run):
        if any(mk in body for mk in marks):
            if pos < len(labels):
                recommended = "%d. %s" % (pos + 1, labels[pos])
            else:
                recommended = "%d. %s" % (pos + 1, body)
            break
    if not recommended:
        for pos, label in enumerate(labels):
            if any(mk in label for mk in marks):
                recommended = "%d. %s" % (pos + 1, label)
                break
    for mk in marks:
        recommended = recommended.replace(mk, "")
    first_opt = run[0][0] if run else marker_idx
    run_idx = set(idx for idx, _ in run)
    prose = [(idx, _clean(line)) for idx, (line, fenced)
             in enumerate(lines[:marker_idx])
             if not fenced and line.strip() and idx not in run_idx
             and not line.strip().startswith("```")
             and not re.match(r"^(?:- )?(Class|Not-How|Kind):", line.strip())]
    prose = [(idx, p) for idx, p in prose if p]
    from why_net import store
    asks = [p for _, p in prose if p.endswith(store.QUESTION_MARKS)]
    before = [p for idx, p in prose if idx < first_opt]
    question = (asks[-1] if asks else before[-1] if before
                else prose[-1][1] if prose else "")
    return {"question": _clip(question, QUESTION_MAX) or "(no question line)",
            "labels": labels,
            "recommended": " ".join(recommended.split())}


# ---------------------------------------------------------------------------
# Current-turn transcript scan
# ---------------------------------------------------------------------------

def current_turn(transcript_path):
    """(boundary key, [tool_use blocks]) of the current turn; (None, [])
    when the transcript is unreadable."""
    entries = []
    try:
        with open(transcript_path, "r", errors="replace") as fh:
            for raw in fh:
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                if entry.get("type") in ("assistant", "user"):
                    entries.append(entry)
    except Exception:
        return None, []
    boundary = None
    for idx in range(len(entries) - 1, -1, -1):
        e = entries[idx]
        injected = bool(e.get("isMeta") or e.get("isSidechain"))
        try:
            genuine = _GATE._is_genuine_user_boundary(
                e.get("type"), injected, e.get("message") or {})
        except Exception:
            genuine = False
        if genuine:
            boundary = idx
            break
    if boundary is None:
        return None, []
    key = entries[boundary].get("uuid") or "%s#%d" % (transcript_path, boundary)
    tools = []
    for e in entries[boundary + 1:]:
        if e.get("type") != "assistant":
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list):
            tools.extend(b for b in content
                         if isinstance(b, dict) and b.get("type") == "tool_use")
    return key, tools


def _same_or_under(path, target, is_dir):
    try:
        real, want = os.path.realpath(path), os.path.realpath(target)
    except Exception:
        return False
    return real == want or (is_dir and real.startswith(want + os.sep))


def turn_wrote_ledger(tools, ledger, dec_dir, cwd):
    """True iff a tool call this turn targeted the ledger or the dir."""
    for block in tools:
        name = block.get("name")
        args = block.get("input") or {}
        if name in _WRITE_TOOLS:
            for key in _PATH_KEYS:
                fp = args.get(key)
                if not isinstance(fp, str) or not fp.strip():
                    continue
                fp = fp if os.path.isabs(fp) else os.path.join(cwd, fp)
                if _same_or_under(fp, ledger, False):
                    return True
                if dec_dir and _same_or_under(fp, dec_dir, True):
                    return True
        elif name == "Bash":
            cmd = args.get("command")
            if isinstance(cmd, str) and _bash_writes(cmd, ledger, dec_dir,
                                                     cwd):
                return True
    return False


_D4 = None


def _bash_writes(cmd, ledger, dec_dir, cwd):
    """True when a Bash command writes the ledger or the per-decision dir
    (DGN-1967 bug 16: a cat / grep that only names the ledger is a read).
    Write targets come from the D4 gate's parser (write-paths-d4
    bash_targets: redirections, tee, sed -i, cp / mv / install ...); an
    inline interpreter naming either counts as a write.  A command the
    parser cannot load for counts as a write, as before."""
    global _D4
    names = [os.path.basename(ledger)]
    if dec_dir:
        names.append(os.path.basename(dec_dir.rstrip(os.sep)) + "/")
    if not any(n in cmd for n in names):
        return False
    try:
        if _D4 is None:
            _D4 = _load("write-paths-d4.py", "_dl_write_paths_d4")
        bs = _D4._bs()
        targets = _D4.bash_targets(cmd, bs)
        segs = list(bs.segments(cmd))
    except Exception:
        return True
    for raw, _verb in targets:
        p = os.path.expanduser(raw)
        p = p if os.path.isabs(p) else os.path.join(cwd, p)
        if _same_or_under(p, ledger, False) or os.path.basename(
                raw.rstrip("/")) == names[0]:
            return True
        if dec_dir and (_same_or_under(p, dec_dir, True)
                        or (raw.rstrip("/") + "/").endswith(names[-1])):
            return True
    for seg in segs:
        name, args, _ = bs.command_words(seg)
        if name in bs.INTERPRETERS:
            code = " ".join(args) + " " + " ".join(seg.heredocs)
            if any(n in code for n in names):
                return True
    return False


# ---------------------------------------------------------------------------
# Ledger I/O (all under the ledger lock)
# ---------------------------------------------------------------------------

class LedgerLock(object):
    def __init__(self, ledger):
        self.ledger = ledger
        self.path = ledger + ".lock"
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        try:
            _repair_pending_journals(self.ledger)
        except BaseException:
            self.fh.close()
            raise
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


id_regex = fmt.id_regex


def next_number(ledger, dec_dir, rx):
    top = 0
    try:
        with open(ledger, encoding="utf-8", errors="replace") as fh:
            for m in rx.finditer(fh.read()):
                top = max(top, int(m.group(1)))
    except OSError:
        pass
    if dec_dir and os.path.isdir(dec_dir):
        for name in os.listdir(dec_dir):
            for m in rx.finditer(name):
                top = max(top, int(m.group(1)))
    return top + 1


def _fmt(template, **fields):
    try:
        return template.format(**fields)
    except Exception:
        return template


def decision_key(owner_id, scope, subject, decision_kind, subject_revision):
    """The registered custody scope is supplied by the owning pack, not a model.

    Event IDs and question wording deliberately do not participate in identity.
    """
    from return_route import canonical, sha256
    fields = [owner_id, scope, subject, decision_kind, subject_revision]
    if any(not isinstance(v, str) or not v.strip() for v in fields):
        raise ValueError("decision identity requires owner, registered scope, subject, kind and revision")
    return sha256(canonical(fields))


def _pending_text(path, text):
    """Durable text replacement used for journal repair."""
    import tempfile
    from return_route import sync_dir
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_dir(parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _repair_pending_journal(path, doc):
    from return_route import atomic_json
    if doc.get("published"):
        return
    try:
        with open(doc["ledger"], encoding="utf-8") as stream:
            current = stream.read()
    except FileNotFoundError:
        current = ""
    if current not in (doc["ledger_before"], doc["ledger_after"]):
        raise ValueError("pending capture journal conflicts with ledger; retain for recovery")
    detail = doc["record"]["file"]
    if os.path.exists(detail):
        with open(detail, encoding="utf-8") as stream:
            if stream.read() != doc["detail"]:
                raise ValueError("pending capture detail conflict; retain for recovery")
    else:
        _pending_text(detail, doc["detail"])
    if current != doc["ledger_after"]:
        _pending_text(doc["ledger"], doc["ledger_after"])
    doc["published"] = True
    atomic_json(path, doc)


def _repair_pending_journals(ledger):
    # Every ledger writer takes LedgerLock, so a reservation is repaired before
    # another writer can allocate its id or change the journal's before-image.
    for path in sorted(glob.glob(ledger + ".pending-once/*.json")):
        with open(path, encoding="utf-8") as stream:
            doc = json.load(stream)
        if not doc.get("published"):
            _repair_pending_journal(path, doc)


def capture_pending_once(key, menu, ledger=None, dec_dir=None, moment=None):
    """Idempotent pending capture, never confirmation. Both documents required.

    The executor persists decision_home and the custody-derived key before
    calling. Replays retain the original menu and accumulate contributing IDs.
    Existing write_pending callers keep their original behavior.
    """
    from return_route import atomic_json, sha256
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("pending capture requires a SHA256 decision key")
    declaration = validate_class(menu)
    labels = menu.get("labels")
    if (not isinstance(labels, list) or not 2 <= len(labels) <= 7
            or any(not isinstance(v, str) or not v.strip() or len(v.splitlines()) != 1 for v in labels)
            or not isinstance(menu.get("question"), str) or not menu["question"].strip()
            or len(menu["question"].splitlines()) != 1
            or menu.get("recommended") not in labels):
        raise ValueError("pending capture requires a question, 2-7 labels and a recommended label")
    paths = ledger_paths() if ledger is None else (ledger, dec_dir)
    if not paths or not paths[1]:
        raise ValueError("pending capture requires a configured ledger and detail home")
    ledger, dec_dir = paths
    ledger, dec_dir = os.path.abspath(ledger), os.path.abspath(dec_dir)
    path = ledger + ".pending-once/" + key + ".json"
    event_ids = menu.get("event_ids", [])
    if not isinstance(event_ids, list) or any(not isinstance(v, str) or not v for v in event_ids):
        raise ValueError("invalid contributing event ids")
    with LedgerLock(ledger):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as stream:
                doc = json.load(stream)
            if doc["ledger"] != ledger or os.path.dirname(doc["record"]["file"]) != dec_dir:
                raise ValueError("decision home changed for a reserved key")
            if not os.path.isfile(doc["record"]["file"]) or not os.path.isfile(ledger):
                raise ValueError("pending capture receipt requires both documents; retain for recovery")
            doc["event_ids"] = sorted(set(doc["event_ids"] + event_ids))
            atomic_json(path, doc)
            return dict(doc["record"], event_ids=doc["event_ids"])
        id_fmt = conf("DECISION_LEDGER_ID_FORMAT")
        dec_id = _fmt(id_fmt, n=next_number(ledger, dec_dir, id_regex(id_fmt)))
        stamps = stamp_fields(moment or now())
        status = _fmt(conf("DECISION_LEDGER_PENDING_STATUS"), **stamps)
        tag = _fmt(conf("DECISION_LEDGER_PENDING_TAG"), status=status, **stamps)
        detail = os.path.join(dec_dir, dec_id + ".md")
        options = fmt.options_text(labels)
        record = dict(id=dec_id, tag=tag, status=status, labels=labels, file=detail, key=key)
        detail_text = "\n".join(["# " + dec_id, "Status: " + status,
            "Question: " + menu["question"], "Class: " + declaration["class"],
            "Not-How: " + declaration["not_how"], "Kind: " + declaration["kind"], "Options: " + options,
            "Recommended: " + menu["recommended"], "Decision-Key: " + key]) + "\n"
        body = "\n".join([_fmt(conf("DECISION_LEDGER_HEADING"), id=dec_id,
            question=menu["question"], tag=tag, **stamps).rstrip(), fmt.mark(dec_id),
            "- Class: " + declaration["class"], "- Not-How: " + declaration["not_how"],
            "- Kind: " + declaration["kind"],
            fmt.OPTIONS_HEAD + options, "- Recommended: " + menu["recommended"],
            "- Decision-Key: " + key, "- Per-decision file: " + detail]) + "\n"
        try:
            with open(ledger, encoding="utf-8") as stream:
                before = stream.read()
        except FileNotFoundError:
            before = ""
        after = before + ("\n" if before.endswith("\n") else "\n\n" if before else "") + body
        doc = dict(schema="pending-capture/1", key=key, ledger=ledger, record=record,
                   event_ids=sorted(set(event_ids)), ledger_before=before,
                   ledger_after=after, detail=detail_text, published=False,
                   intended_sha256=sha256((after + detail_text).encode()))
        atomic_json(path, doc)  # Reserve id + exact bytes BEFORE either document.
        _repair_pending_journal(path, doc)
        return dict(record, event_ids=doc["event_ids"])


def write_pending(ledger, dec_dir, menu, moment):
    """Allocate an id and write the entry + per-decision file; return the
    pointer record."""
    declaration = validate_class(menu)
    id_fmt = conf("DECISION_LEDGER_ID_FORMAT")
    rx = id_regex(id_fmt)
    stamps = stamp_fields(moment)
    status = _fmt(conf("DECISION_LEDGER_PENDING_STATUS"), **stamps)
    tag = _fmt(conf("DECISION_LEDGER_PENDING_TAG"), status=status, **stamps)
    options = fmt.options_text(menu["labels"])
    recommended = menu["recommended"] or "(none marked)"
    with LedgerLock(ledger):
        n = next_number(ledger, dec_dir, rx)
        dec_file = fh = None
        while dec_dir:
            os.makedirs(dec_dir, exist_ok=True)
            dec_file = os.path.join(dec_dir, _fmt(id_fmt, n=n) + ".md")
            try:
                fh = os.fdopen(os.open(
                    dec_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644),
                    "w", encoding="utf-8")
                break
            except FileExistsError:
                n += 1
        dec_id = _fmt(id_fmt, n=n)
        try:
            if fh is not None:
                with fh:
                    fh.write("\n".join([
                        "# %s" % dec_id,
                        "Status: %s" % status,
                        "Question: %s" % menu["question"],
                        "Class: %s" % declaration["class"],
                        "Not-How: %s" % declaration["not_how"],
                        "Kind: %s" % declaration["kind"],
                        "Options: %s" % options,
                        "Recommended: %s" % recommended,
                        "See %s %s." % (os.path.basename(ledger), dec_id),
                    ]) + "\n")
            heading = _fmt(conf("DECISION_LEDGER_HEADING"), id=dec_id,
                           question=menu["question"], tag=tag, **stamps)
            body = [heading.rstrip(),
                    fmt.mark(dec_id),
                    "- Class: %s" % declaration["class"],
                    "- Not-How: %s" % declaration["not_how"],
                    "- Kind: %s" % declaration["kind"],
                    fmt.OPTIONS_HEAD + options,
                    "- Recommended: %s" % recommended]
            if dec_file:
                body.append("- Machine-recorded from the decision menu; "
                            "per-decision file %s."
                            % os.path.relpath(dec_file, ROOT))
            else:
                body.append("- Machine-recorded from the decision menu.")
            prefix = ""
            try:
                if os.path.getsize(ledger) > 0:
                    with open(ledger, "rb") as lf:
                        lf.seek(-1, os.SEEK_END)
                        prefix = "\n" if lf.read(1) == b"\n" else "\n\n"
            except OSError:
                pass
            chunk = (prefix + "\n".join(body) + "\n").encode("utf-8")
            afd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                          0o644)
            try:
                os.write(afd, chunk)
            finally:
                os.close(afd)
        except BaseException:
            if dec_file:
                try:
                    os.remove(dec_file)
                except OSError:
                    pass
            raise
    return {"id": dec_id, "tag": tag, "status": status,
            "labels": menu["labels"],
            "file": dec_file or ""}


def _replace_file(path, text):
    from why_net import store  # the one routines-side atomic writer
    store.atomic_write(path, text, private=False)


def inbound_request_id(prompt, now_ts=None):
    """The bridge's request id for THIS prompt, or None (absent, stale, or
    naming another prompt: fail closed -- no req= is written).  The reader
    is decision_ledger_fmt's, shared with the why-net ups hook."""
    import time
    inbound = fmt.read_inbound(_data_dir(),
                               time.time() if now_ts is None else now_ts)
    return fmt.request_id_for(inbound, prompt)


def _heading_rx(template):
    """Regex over a rendered DECISION_LEDGER_HEADING: one named group per
    field ({tag} greedy, the rest lazy)."""
    parts = re.split(r"\{(\w+)\}", template)
    seen, out = set(), []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(re.escape(part))
        elif part in seen:
            out.append(".*?")
        else:
            seen.add(part)
            out.append("(?P<%s>%s)" % (part, ".*" if part == "tag" else ".*?"))
    return re.compile("^" + "".join(out).rstrip() + r"\s*$")


def record_for(dec_id):
    """F13 seam (public for devkit): the full record of ledger entry `dec_id`
    as confirm() takes it -- {id, state, tag, question, labels, file} -- or
    None when no entry carries the id.  tag is the heading's own tag text
    (so confirm() replaces it instead of appending a second one); state is
    pending|confirmed (machine mark) or recorded (no mark).  Read-only."""
    paths = ledger_paths()
    if paths is None:
        return None
    ledger, dec_dir = paths
    try:
        with open(ledger, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return None
    rx = id_regex(conf("DECISION_LEDGER_ID_FORMAT"))
    head_rx = _heading_rx(conf("DECISION_LEDGER_HEADING"))
    start = None
    for i in range(len(lines) - 1, -1, -1):
        m = fmt.MARK_RE.search(lines[i])
        if m and m.group(1) == dec_id:
            start = i - 1
            break
    state = "confirmed" if start is not None and "state=confirmed" in lines[start + 1] else "pending"
    if start is None:
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("#"):
                m = rx.search(lines[i])
                if m and m.group(0) == dec_id:
                    start, state = i, "recorded"
                    break
    if start is None or start < 0:
        return None
    head = lines[start]
    hm = head_rx.match(head)
    tag = (hm.groupdict().get("tag") or "").strip() if hm else ""
    question = (hm.groupdict().get("question") or "").strip() if hm else ""
    labels = []
    for line in lines[start + 1:start + 6]:
        if line.startswith("#"):
            break
        got = fmt.options_of_line(line)
        if got is not None:
            labels = got
            break
    dec_file = ""
    if dec_dir:
        cand = os.path.join(dec_dir, dec_id + ".md")
        if os.path.isfile(cand):
            dec_file = cand
            if not question:
                with open(cand, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.startswith("Question: "):
                            question = line[len("Question: "):].strip()
                            break
    return {"id": dec_id, "state": state, "tag": tag, "question": question,
            "labels": labels, "file": dec_file}


def confirm(ledger, record, answer, moment, req=None):
    stamps = stamp_fields(moment)
    status = _fmt(conf("DECISION_LEDGER_CONFIRMED_STATUS"), **stamps)
    tag = _fmt(conf("DECISION_LEDGER_CONFIRMED_TAG"), status=status,
               answer=answer, **stamps)
    with LedgerLock(ledger):
        with open(ledger, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        mark = fmt.mark(record["id"])
        idx = next((i for i in range(len(lines) - 1, -1, -1)
                    if lines[i].strip() == mark), None)
        if idx is None:
            return False
        lines[idx] = fmt.mark(record["id"], "confirmed", req)
        if idx > 0:
            head = lines[idx - 1]
            old = record.get("tag") or ""
            if old and old in head:
                cut = head.rfind(old)
                head = head[:cut] + tag + head[cut + len(old):]
            else:
                head = head.rstrip() + " " + tag
            lines[idx - 1] = head
        _replace_file(ledger, "\n".join(lines))
        dec_file = record.get("file")
        if dec_file and os.path.isfile(dec_file):
            with open(dec_file, encoding="utf-8") as fh:
                flines = fh.read().split("\n")
            for i, line in enumerate(flines):
                if line.startswith("Status: "):
                    flines[i] = "Status: %s" % status
                    flines.insert(i + 1, 'Answer: "%s"' % answer)
                    break
            _replace_file(dec_file, "\n".join(flines))
    return True


# ---------------------------------------------------------------------------
# Pending pointer (the immediately-previous-turn link)
# ---------------------------------------------------------------------------

def _pointer_path():
    return os.path.join(_data_dir(), POINTER_FILENAME)


def read_pointer():
    try:
        with open(_pointer_path(), encoding="utf-8") as fh:
            rec = json.load(fh)
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def write_pointer(rec):
    path = _pointer_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _replace_file(path, json.dumps(rec, ensure_ascii=False))


def clear_pointer():
    try:
        os.remove(_pointer_path())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Answer matching
# ---------------------------------------------------------------------------

def answers_menu(prompt, labels):
    """True when `prompt` picks one option (decision_ledger_fmt.pick: a bare
    number in range, "N. <that label>", a label, or a number handle)."""
    return fmt.answers_menu(prompt, labels)


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------

def run_stop(data):
    paths = ledger_paths()
    if paths is None or not is_owner_session(data):
        return
    ledger, dec_dir = paths
    final = data.get("last_assistant_message")
    final = final if isinstance(final, str) else ""
    key, tools = current_turn(data.get("transcript_path") or "")
    sid = data.get("session_id") or ""
    turn = key or ""
    has_menu = bool(final) and _DRF.has_options_marker(final)
    pointer = read_pointer()
    same_turn = bool(pointer and turn and pointer.get("turn") == turn
                     and pointer.get("session_id") == sid)
    if not has_menu:
        if not same_turn:
            clear_pointer()
        return
    # Validate before either deduplication or the agent-wrote shortcut.
    declarations = menu_classes(final, tools, ledger, dec_dir,
                                data.get("cwd") or ROOT,
                                pointer.get("id") if same_turn else None)
    if same_turn:
        # A re-prompted Stop of the turn already recorded: never duplicate.
        return
    if turn_wrote_ledger(tools, ledger, dec_dir, data.get("cwd") or ROOT):
        clear_pointer()
        log("SKIP_AGENT_WROTE", "turn=%s" % turn)
        return
    menu = parse_menu(final)
    if menu is None:
        return
    menu.update(declarations[-1])
    rec = write_pending(ledger, dec_dir, menu, now())
    rec.update({"session_id": sid, "turn": turn})
    write_pointer(rec)
    log("PENDING", "id=%s labels=%d" % (rec["id"], len(rec["labels"])))


BRIDGE_CLASS = {"class": "What",
                "not_how": "owner menu sent by the bridge; the agent declared "
                           "no purpose line",
                "kind": "fork"}


def run_send(text):
    """Bridge send-time capture. Returns the pointer record or None."""
    import hashlib
    paths = ledger_paths()
    if paths is None or not isinstance(text, str) or not text:
        return None
    if not (_DRF.has_options_marker(text) if _DRF else False):
        return None
    menu = parse_menu(text)
    if menu is None or not 2 <= len(menu["labels"]) <= 10:
        return None
    send_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ledger, dec_dir = paths
    pointer = read_pointer()
    if pointer and pointer.get("send_key") == send_key:
        return pointer
    try:
        declaration = class_fields(text)
    except ClassRefused:
        declaration = dict(BRIDGE_CLASS)
    menu.update(declaration)
    rec = write_pending(ledger, dec_dir, menu, now())
    rec.update({"session_id": "", "turn": "", "send_key": send_key})
    write_pointer(rec)
    log("PENDING", "id=%s labels=%d via=bridge-send" % (rec["id"], len(rec["labels"])))
    return rec


def decision_blocks(text):
    """Heading entries and the original compact pending-line format."""
    rx = id_regex(conf("DECISION_LEDGER_ID_FORMAT"))
    blocks, current = [], None
    for line, fenced in _unfenced_lines(text):
        if fenced:
            continue
        start = (line.startswith("#") or
                 re.match(r"^- \[\d{4}-\d\d-\d\d\]", line))
        if start:
            if current:
                blocks.append(current)
            match = rx.search(line)
            current = [match.group(0), line] if match else None
        elif current:
            current[1] += "\n" + line
    if current:
        blocks.append(current)
    return blocks


def legacy_detail_id(ledger, path):
    """Authorize history from the existing ledger, never the proposed file."""
    ident, extension = os.path.splitext(os.path.basename(path))
    if extension != ".md" or not id_regex(
            conf("DECISION_LEDGER_ID_FORMAT")).fullmatch(ident):
        return None
    try:
        with open(ledger, encoding="utf-8") as fh:
            headings = [body.splitlines()[0] for key, body in
                        decision_blocks(fh.read()) if key == ident]
    except OSError:
        return None
    if len(headings) != 1 or not headings[0].startswith("#"):
        return None
    # Respect configured heading formats; older headings may predate that
    # template but still use the original parenthesized decision date.
    match = _heading_rx(conf("DECISION_LEDGER_HEADING")).match(headings[0])
    date = match.groupdict().get("date") if match else None
    if not date:
        match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", headings[0])
        date = match[1] if match else ""
    try:
        if datetime.date.fromisoformat(date) < CLASS_RULE_DATE:
            return ident
    except ValueError:
        pass
    return None


def check_raise(before, after, legacy_id=None):
    """Existing history need not be reclassified to confirm an old decision."""
    from collections import Counter
    old = Counter(ident for ident, _ in decision_blocks(before))
    for ident, body in decision_blocks(after):
        if old[ident]:
            old[ident] -= 1
        else:
            class_fields(body, allow_legacy=(ident == legacy_id))


def run_pre(data):
    paths = ledger_paths() or (os.path.join(ROOT, "worklog", "_DECISIONS.md"),
                               os.path.join(ROOT, "worklog", "decisions"))
    ledger, dec_dir = paths
    name = data.get("tool_name")
    args = data.get("tool_input") or {}
    cwd = data.get("cwd") or ROOT
    block = {"name": name, "input": args}
    if not turn_wrote_ledger([block], ledger, dec_dir, cwd):
        return
    if name in ("Bash", "NotebookEdit"):
        raise ClassRefused(CLASS_HELP + " Use Write/Edit/MultiEdit for ledger "
                           "changes so the proposed entries can be checked.")
    raw = next((args[k] for k in _PATH_KEYS if args.get(k)), "")
    path = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
    try:
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
    except FileNotFoundError:
        before = ""
    except OSError:
        raise ClassRefused(CLASS_HELP + " Cannot read the existing entry.")
    after = before
    if name == "Write":
        after = args.get("content", "")
    else:
        edits = args.get("edits", []) if name == "MultiEdit" else [args]
        for edit in edits:
            old, new = edit.get("old_string", ""), edit.get("new_string", "")
            if not old or old not in after:
                raise ClassRefused(CLASS_HELP + " Cannot verify this edit.")
            after = after.replace(old, new, -1 if edit.get("replace_all") else 1)
    legacy_id = None
    has_legacy = any(not fenced and re.fullmatch(
        r"\s*(?:- )?Class:\s*legacy\s*", line)
        for line, fenced in _unfenced_lines(after))
    if has_legacy:
        if (dec_dir and _same_or_under(path, dec_dir, True)
                and not _same_or_under(path, ledger, False)):
            legacy_id = legacy_detail_id(ledger, path)
        class_fields(after, allow_legacy=bool(legacy_id))
        if any(ident != legacy_id for ident, _ in decision_blocks(after)):
            raise ClassRefused(CLASS_HELP + " Legacy detail id must match its ledger heading.")
    check_raise(before, after, legacy_id=legacy_id)
    # A newly created per-decision file may use frontmatter instead of a
    # heading. It still needs a declaration; arbitrary notes elsewhere do not.
    if not before and dec_dir and _same_or_under(path, dec_dir, True):
        if id_regex(conf("DECISION_LEDGER_ID_FORMAT")).search(os.path.basename(path)):
            class_fields(after, allow_legacy=bool(legacy_id))


def run_ups(data):
    paths = ledger_paths()
    if paths is None or not is_owner_session(data):
        return
    pointer = read_pointer()
    if not pointer:
        return
    full = data.get("prompt")
    full = full if isinstance(full, str) else ""
    # DGN-1967 bug 2: the pushes the bridge prefixes to the owner's turn are
    # the agent's words; the full text only binds the bridge request id.
    prompt = _OC.strip_block(full) if _OC is not None else full
    if prompt.lstrip().startswith(INJECTED_TURN_MARK):
        return  # a machine turn, not the user's answer: keep the pointer
    clear_pointer()
    # A bridge-written pointer (send seat) has no session; it binds to the
    # next owner prompt, like the Stop-written pointer did within its session.
    if pointer.get("session_id") not in ("", None) and \
            pointer.get("session_id") != (data.get("session_id") or ""):
        log("LEAVE_PENDING", "id=%s other-session" % pointer.get("id"))
        return
    if not answers_menu(prompt, pointer.get("labels") or []):
        log("LEAVE_PENDING", "id=%s not-an-answer" % pointer.get("id"))
        return
    answer = _clip(" ".join(prompt.strip().split()), ANSWER_MAX)
    if confirm(paths[0], pointer, answer, now(), inbound_request_id(full)):
        log("CONFIRMED", "id=%s" % pointer.get("id"))
    else:
        log("LEAVE_PENDING", "id=%s entry-gone" % pointer.get("id"))


# ---------------------------------------------------------------------------
# Restart handover (DGN-1420)
# ---------------------------------------------------------------------------
# A bridge restart cuts the live conversation; a define the owner confirmed
# an hour earlier then exists only in chat history, and the resumed session
# re-ran it and got the opposite verdict.  The restart worker
# (bridge/self_restart.sh drop_verify_spool) asks this seat for the latest
# ledger entries and hands them to the resumed session as step 5 of the
# post-restart spool: in-flight DECISION state, next to in-flight work.

HANDOVER_DEFAULT_COUNT = 10
HEAD_MAX = 200
_ANY_MARK_RE = fmt.MARK_RE


def handover_entries(limit):
    """The last `limit` ledger entries, oldest first: [{state, head}].
    state = confirmed | pending (machine mark) | recorded (agent-written,
    no mark: its heading carries whatever status the agent wrote)."""
    paths = ledger_paths()
    if not paths:
        return []
    rx = id_regex(conf("DECISION_LEDGER_ID_FORMAT"))
    try:
        with open(paths[0], encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return []
    entries, cur = [], None
    for line in lines:
        if line.startswith("#"):
            cur = None
            if rx.search(line):
                cur = {"state": "recorded",
                       "head": _clip(line.lstrip("#").strip(), HEAD_MAX)}
                entries.append(cur)
            continue
        m = _ANY_MARK_RE.search(line)
        if cur is not None and m:
            cur["state"] = m.group(2)
    return entries[-limit:] if limit > 0 else []


def handover_text():
    """The spool step, or "" (ledger off, missing or empty)."""
    try:
        limit = int(conf("DECISION_HANDOVER_COUNT")
                    or HANDOVER_DEFAULT_COUNT)
    except ValueError:
        limit = HANDOVER_DEFAULT_COUNT
    entries = handover_entries(limit)
    if not entries:
        return ""
    ledger = os.path.relpath(ledger_paths()[0], ROOT)
    rows = "\n".join("     - [%s] %s" % (e["state"], e["head"])
                     for e in entries)
    return "\n".join([
        "5. Carry the owner's decisions -- the restart cut the conversation,"
        " not the",
        "   decisions made in it. \"No in-flight task\" above covers running"
        " work only.",
        "   Latest entries of the decision ledger (%s):" % ledger,
        rows,
        "   confirmed / recorded = the owner's verdict: build on it. Never"
        " re-run a",
        "   define or scope it settles and never re-ask its question; if new"
        " work",
        "   seems to contradict it, show the owner the recorded verdict"
        " before",
        "   proposing a change. pending = asked, not yet answered: re-surface"
        " it,",
        "   do not decide it anew. Read the full entry in the ledger before"
        " acting on it.",
    ])


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--capabilities":
        print("capture-pending-once/1")
        return
    if mode == "handover":
        try:
            text = handover_text()
            if text:
                sys.stdout.write(text + "\n")
        except Exception as exc:
            log("SKIP", "mode=handover error=%s: %s" % (
                type(exc).__name__, str(exc)[:200]))
        return
    if mode == "send":
        try:
            data = json.load(sys.stdin)
            rec = run_send(data.get("text")) if isinstance(data, dict) else None
            if rec:
                sys.stdout.write(json.dumps(
                    {"id": rec["id"], "labels": rec["labels"]}) + "\n")
        except Exception as exc:
            log("SKIP", "mode=send error=%s: %s" % (
                type(exc).__name__, str(exc)[:200]))
        return
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return
        if mode == "pre":
            return  # Class declarations are optional send-time metadata.
        elif mode == "stop":
            return  # Menus are recorded by the bridge send seat.
        elif mode == "ups":
            run_ups(data)
    except ClassRefused as exc:
        refuse(mode, str(exc))
    except Exception as exc:
        log("SKIP", "mode=%s error=%s: %s" % (mode, type(exc).__name__,
                                              str(exc)[:200]))


if __name__ == "__main__":
    # Audit commands fail closed and keep their exit status. Hook modes retain
    # the existing fail-open contract and never import the optional reader.
    if len(sys.argv) > 1 and sys.argv[1] == "validation":
        from decision_validation import cli
        sys.exit(cli(sys.modules[__name__], sys.argv[2:]))
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
