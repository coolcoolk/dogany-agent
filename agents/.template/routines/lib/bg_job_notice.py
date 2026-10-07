#!/usr/bin/env python3
"""Owner notices for session background jobs (DGN-1691 phase 2).

WHY: owner 2026-09-24 "다른 백 작업들도 동생보내듯이 규약 맞춰줘 displayname
time start& end push etc".  A dispatched sibling tells the owner when it
starts and when it ends; a background Bash / Monitor / Workflow job launched
by the owner session told him nothing.  This module gives such a job the SAME
protocol, on the SAME rails:

  display name  the launching tool_use's `description`, which the PreToolUse
                gate (routines/bg-job-notice.py --gate) requires to be written
                in the instance language for a background launch -- the one
                decision point where the name can still be corrected (the
                dispatch rail's --display-name refusal, applied here).
  START push    dispatch-detached.sh --start-notice-enqueue: start_notice_arm
                itself, so a shell job joins the dispatch queue, flusher and
                join window (one `▶️ ... 작업 시작 (N건)` per burst).
  END push      dispatch-detached.sh --end-notice: finalize_run's own
                renderer (✅ block on completed, the ⚠️ lines otherwise).
  model slot    model_slot() below (a shell job has no model): an INTERNAL
                field -- state, --model-slot, the no-description fallback --
                never part of the owner-visible name (DGN-1820).

Exactly-once: one small JSON state file keyed by task id, mutated under an
flock.  START is recorded before it is sent and never re-sent; END is sent
only for a task whose START was sent, and only once -- so the Stop hook can
re-scan the whole transcript on every turn without repeating a notice, and
history from before this module existed never produces a notice at all.

Exclusions: only Bash (run_in_background / auto-moved / manual), Monitor and
Workflow launches are candidates.  Scheduled tools (cron / remote triggers)
and launchd routines never reach this module -- periodic work never notifies.

Pure stdlib.  Every entry point is fail-open: a notice problem must never
break or delay the owner's turn.
"""

import json
import os
import re
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

_LIB_DIR = os.path.dirname(os.path.realpath(__file__))
_ROUTINES_DIR = os.path.dirname(_LIB_DIR)
_ROOT_DIR = os.path.dirname(_ROUTINES_DIR)

sys.path.insert(0, _LIB_DIR)
try:
    from conf_reader import conf_get
except Exception:  # pragma: no cover - lib damaged
    def conf_get(key, conf_path=None):
        return ""

# The <Model> slot for a job that has no model -- OWNER-FACING COPY, draft
# pending owner confirmation (candidates in the DGN-1691 phase 2 report).
# Keyed by launching tool: the slot answers "who is doing this" the way the
# model name does on a dispatch row.
BG_MODEL_SLOT = {"Bash": "Shell", "Monitor": "Monitor", "Workflow": "Workflow"}
# Owner ruling 2026-09-24 16:08 ("cli 이름이나"): a background shell job's
# model slot is the CLI it runs (git, python3, bash ...); BG_MODEL_SLOT is
# only the fallback when no command name can be read.
_WRAPPERS = ("env", "sudo", "nohup", "time", "exec", "command", "caffeinate")
_SHELL_KEYWORDS = frozenset((
    "for", "while", "until", "if", "then", "else", "elif", "do", "done",
    "fi", "case", "esac", "select", "function", "time", "!", "{", "}",
))
# A loop/conditional header is not a command: its words are variables or
# tests.  Body prefixes, on the other hand, can share a segment with the
# command they introduce ("do python3 ...", "! grep ...").
_SHELL_HEADER_KEYWORDS = frozenset((
    "for", "while", "until", "if", "elif", "case", "select", "function",
))
_SHELL_END_KEYWORDS = frozenset(("done", "fi", "esac"))
_SHELL_BODY_PREFIXES = frozenset(("then", "else", "do", "!"))
# DGN-1769 (owner 2026-09-28 07:08): `bash <dir>/release.sh` is the release
# script at work, not "bash" -- an interpreter running a script file hands
# the slot to the script's basename.  Inline code (-c, python -m) or no
# script operand keeps the interpreter name.
_INTERPRETER_RE = re.compile(r"^(?:ba|z|da|k)?sh$|^python(?:3(?:\.\d+)?)?$")
_INLINE_FLAGS = frozenset(("-c", "-m", "-"))


def _script_operand(toks):
    """Basename of the script an interpreter runs (toks[0] is the
    interpreter), or None for inline code / no operand."""
    for tok in toks[1:]:
        if tok in _INLINE_FLAGS or tok.startswith("-c"):
            return None
        if tok.startswith("-"):
            continue
        name = os.path.basename(tok)
        return name or None
    return None


# DGN-1820: a token that can name a command (git, release.sh, python3.12,
# g++).  Anything else at a command position is shell residue.
_SLOT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]*$")


def _blank_substitutions(cmd):
    """`cmd` with every $(...), $((...)) and `...` replaced by one "_", so an
    assignment like S=$(date +%s) stays a single word and the separators
    inside a substitution never split the outer command (DGN-1820: the
    slot read "+%s)").  Quotes are honoured; unbalanced input is blanked to
    its end."""
    out = []
    i, n = 0, len(cmd)
    quote = None
    while i < n:
        ch = cmd[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if ch == "$" and cmd.startswith("(", i + 1):
            depth, j, inner = 0, i + 1, None
            while j < n:
                c = cmd[j]
                if c == "\\":
                    j += 2
                    continue
                if inner:
                    if c == inner:
                        inner = None
                elif c in "'\"":
                    inner = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append("_")
            i = j + 1
            continue
        if ch == "`":
            j = cmd.find("`", i + 1)
            out.append("_")
            i = n if j < 0 else j + 1
            continue
        if ch == '"':
            quote = None if quote == '"' else '"'
        elif ch == "'" and quote is None:
            quote = "'"
        out.append(ch)
        i += 1
    return "".join(out)


def model_slot(tool, tool_input=None):
    """Slot text for a background job: the CLI name for Bash, else the tool."""
    fallback = BG_MODEL_SLOT.get(tool) or tool
    if tool != "Bash" or not isinstance(tool_input, dict):
        return fallback
    cmd = tool_input.get("command")
    if not isinstance(cmd, str):
        return fallback
    import shlex
    for seg in re.split(r"&&|\|\||;|\n|\||(?<![<>&])&(?![>&])",
                        _blank_substitutions(cmd)):
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            toks = seg.split()
        # Group delimiters can be standalone tokens.  Drop only delimiters,
        # rather than stripping arbitrary punctuation from a command name.
        while toks and toks[0] in ("(", "{"):
            toks.pop(0)
        while toks and toks[-1] in (")", "}"):
            toks.pop()
        # Subshell parens glued to a word: "(cd d" / "r.py)" (DGN-1820).
        toks = [t.lstrip("(").rstrip(")") for t in toks]
        toks = [t for t in toks if t]
        if not toks:
            continue
        if toks[0] in _SHELL_HEADER_KEYWORDS or toks[0] in _SHELL_END_KEYWORDS:
            continue
        while toks and toks[0] in _SHELL_BODY_PREFIXES:
            toks.pop(0)
        while toks and ("=" in toks[0] and not toks[0].startswith("-")
                        or toks[0] in _WRAPPERS or toks[0].startswith("-")):
            # env -u NAME / -C DIR / -S STR take a value
            drop = 2 if toks[0] in ("-u", "-C", "-S") else 1
            toks = toks[drop:]
        if (not toks or toks[0] in _SHELL_KEYWORDS
                or toks[0] in ("cd", "export", "set", "source", ".")):
            continue
        name = os.path.basename(toks[0])
        if _INTERPRETER_RE.match(name):
            script = _script_operand(toks)
            if script and _SLOT_NAME_RE.match(script):
                name = script
        # Only a command-name-shaped token is a slot; shell residue ("+%s)",
        # "$X", "2>&1") is skipped, never returned (DGN-1820).
        if _SLOT_NAME_RE.match(name):
            return name
    return fallback
BG_TOOLS = tuple(BG_MODEL_SLOT)

# DGN-1820 (owner 2026-10-01, supersedes the DGN-1727 "<slot>: <description>"
# label): the owner-visible name of a background job -- START, END, workbench
# row -- is its description alone.  The slot is machine plumbing (a command
# head; "+%s)" for S=$(date +%s); ...) and stays an internal field.  It is
# shown only when there is no description at all, so a row is never blank.


def owner_label(slot, text):
    """The owner-visible job name: `text`, or the slot when `text` is empty."""
    text = (text or "").strip()
    return text or (slot or "").strip()


def _unlabelled(name, slot):
    """A state entry written before DGN-1820 stored "<slot>: <description>";
    its END must not carry the prefix either."""
    name = (name or "").strip()
    prefix = "%s: " % (slot or "").strip()
    if slot and name.startswith(prefix) and name[len(prefix):].strip():
        return name[len(prefix):].strip()
    return name

STATE_NAME = "bg-notice-state.json"
LOG_NAME = "bg-notice.log"
# Sent-state retention: a finished entry only has to outlive Stop-hook
# re-scans of the same transcript; an unfinished one outlives any real job.
DONE_RETENTION_SECS = 3 * 24 * 3600
OPEN_RETENTION_SECS = 14 * 24 * 3600

# Characters of the instance's own script, per AGENT_LANG base code.  A
# language absent here (or English) is checked for presence only: there is
# no reliable script test for it, and guessing would refuse good names.
_LANG_SCRIPTS = {
    "ko": ((0xAC00, 0xD7A3), (0x1100, 0x11FF), (0x3130, 0x318F)),
    "ja": ((0x3040, 0x30FF), (0x4E00, 0x9FFF)),
    "zh": ((0x4E00, 0x9FFF),),
}

# First-line launch acknowledgements (same shapes status-footer.py pairs in
# the transcript); used only when the tool_response carries no id field.
_ACK_RE = re.compile(
    r"^(?:Command running in background with ID: |"
    r"Command was manually backgrounded by user with ID: |"
    r"Command did not complete .*?moved to the background \(ID: |"
    r"Monitor started \(task |"
    r"Workflow launched in (?:background|a remote CCR session)\. Task ID: )"
    r"([A-Za-z0-9_-]+)")
_RESPONSE_ID_KEYS = ("backgroundTaskId", "taskId", "task_id")


def start_window():
    """The dispatch start-notice join window (dispatch-detached.sh
    START_NOTICE_WINDOW: same env override, same measured default)."""
    val = os.environ.get("DOGANY_START_NOTICE_WINDOW") or ""
    return int(val) if val.isdigit() else 8


def _bot_data_dir():
    return os.environ.get("DOGANY_BOT_DATA_DIR") or os.path.join(
        _ROOT_DIR, ".telegram_bot")


def _dispatch_script():
    return os.environ.get("DOGANY_BG_NOTICE_DISPATCH") or os.path.join(
        _ROUTINES_DIR, "dispatch-detached.sh")


def owner_lang():
    """AGENT_LANG base code -- the key dispatch-detached.sh resolve_owner_lang
    reads (config/agent.conf, en default)."""
    path = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(
        _ROOT_DIR, "config", "agent.conf")
    lang = (conf_get("AGENT_LANG", path) or "en").strip().lower()
    return re.split(r"[-_.]", lang, maxsplit=1)[0] or "en"


def _identifier_hits(name):
    """Execution identifiers in `name` (the send rail's own detector);
    () when the detector is unavailable -- push.sh fails open the same way."""
    try:
        if _ROOT_DIR not in sys.path:
            sys.path.insert(0, _ROOT_DIR)
        from bridge.machine_gate import find_execution_identifiers
        return tuple(find_execution_identifiers(name))
    except Exception:
        return ()


def name_problem(name, lang=None):
    """None when `name` may be rendered to the owner, else an English reason."""
    lang = lang or owner_lang()
    text = (name or "").strip()
    if not text:
        return "the description is empty"
    if any(ch in text for ch in "\t\n\r|"):
        return "the description carries a tab, newline or '|'"
    ranges = _LANG_SCRIPTS.get(lang)
    if ranges and not any(lo <= ord(ch) <= hi for ch in text
                          for lo, hi in ranges):
        return ("the description is not written in the instance language "
                "(AGENT_LANG=%s)" % lang)
    hits = _identifier_hits(text)
    if hits:
        return ("the description carries an execution identifier (%s) that "
                "the owner push rail refuses" % ", ".join(k for k, _ in hits))
    return None


def is_background_launch(tool_name, tool_input):
    """True for a launch that is background by request.  Bash only when
    run_in_background is set; Monitor and Workflow always run detached."""
    if tool_name == "Bash":
        return bool((tool_input or {}).get("run_in_background"))
    return tool_name in ("Monitor", "Workflow")


def launch_name(tool_input):
    inp = tool_input if isinstance(tool_input, dict) else {}
    return (inp.get("description") or inp.get("summary") or "").strip()


def launched_task_id(tool_response):
    """Task id of a background launch from a PostToolUse tool_response, or
    None when the response does not acknowledge one.  Measured shapes: Bash
    {"backgroundTaskId": ...} (explicit and auto-moved), Monitor
    {"taskId": ...}; plain-text acks are read from their first line only."""
    if isinstance(tool_response, dict):
        for key in _RESPONSE_ID_KEYS:
            val = tool_response.get(key)
            if isinstance(val, str) and re.match(r"^[A-Za-z0-9_-]+$", val):
                return val
        texts = [v for v in tool_response.values() if isinstance(v, str)]
    elif isinstance(tool_response, str):
        texts = [tool_response]
    elif isinstance(tool_response, list):
        texts = [b.get("text") for b in tool_response
                 if isinstance(b, dict) and isinstance(b.get("text"), str)]
    else:
        texts = []
    for txt in texts:
        m = _ACK_RE.match(txt.lstrip().split("\n", 1)[0])
        if m:
            return m.group(1)
    return None


def launch_start_epoch(tool_response, now=None):
    """Launch time: now, or earlier by the foreground time an auto-moved
    command already ran (timedOutAfterMs)."""
    now = time.time() if now is None else now
    try:
        waited = float((tool_response or {}).get("timedOutAfterMs") or 0)
    except (AttributeError, TypeError, ValueError):
        waited = 0.0
    return now - max(waited, 0.0) / 1000.0


# ---------------------------------------------------------------------------
# Sent-state
# ---------------------------------------------------------------------------

def state_path():
    return os.environ.get("DOGANY_BG_NOTICE_STATE") or os.path.join(
        _bot_data_dir(), STATE_NAME)


class _Locked(object):
    """flock'd read-modify-write of the state file (lock-free without fcntl)."""

    def __enter__(self):
        path = state_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._fh = open(path + ".lock", "a")
        if fcntl is not None:
            fcntl.flock(self._fh, fcntl.LOCK_EX)
        try:
            with open(path) as fh:
                data = json.load(fh)
            self.state = data if isinstance(data, dict) else {}
        except Exception:
            self.state = {}
        return self

    def save(self, now):
        for tid in list(self.state):
            rec = self.state[tid]
            if not isinstance(rec, dict):
                del self.state[tid]
                continue
            age = now - float(rec.get("recorded") or 0)
            if age > (DONE_RETENTION_SECS if rec.get("end_sent")
                      else OPEN_RETENTION_SECS):
                del self.state[tid]
        path = state_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)

    def __exit__(self, *exc):
        try:
            if fcntl is not None:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
        return False


def _log(msg):
    try:
        with open(os.path.join(_bot_data_dir(), LOG_NAME), "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except Exception:
        pass


def _spawn(argv):
    """Detached sender (own session): a hook timeout or the turn ending must
    not take the notice with it."""
    try:
        log = open(os.path.join(_bot_data_dir(), LOG_NAME), "a")
    except Exception:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen([_dispatch_script()] + argv, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True,
                         close_fds=True)
        return True
    except Exception as exc:
        _log("sender spawn failed: %s" % exc)
        return False
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def record_start(task_id, tool, name, start_epoch, now=None, sender=None,
                 slot=None):
    """Queue the START notice for `task_id` once.  Returns the outcome:
    "sent", "dup", "refused" (name fails name_problem -- recorded, so neither
    START nor END is ever sent for it) or "skip" (not a candidate)."""
    now = time.time() if now is None else now
    if not task_id or tool not in BG_MODEL_SLOT:
        return "skip"
    sender = sender or _spawn
    with _Locked() as lk:
        if task_id in lk.state:
            return "dup"
        problem = name_problem(name)
        rec = {"tool": tool, "name": name, "start": start_epoch,
               "slot": slot or model_slot(tool),
               "recorded": now, "queued": now, "start_sent": problem is None,
               "end_sent": False}
        if problem:
            rec["refused"] = problem
        if not problem:
            # Stored as the owner sees it so the END names the same subject.
            name = rec["name"] = owner_label(rec["slot"], name)
        lk.state[task_id] = rec
        lk.save(now)
    if problem:
        _log("start notice skipped for %s: %s" % (task_id, problem))
        return "refused"
    sender(["--start-notice-enqueue", "--display-name", name,
            "--model-slot", rec["slot"]])
    return "sent"


def started_open_ids():
    """Task ids whose START was sent and whose END was not: jobs the owner
    was told began and has not been told ended.  The workbench keeps such a
    job listed past its launch-age ceiling (DGN-1769).  Fail-open: any state
    problem -> empty set (the board falls back to the launch-age rule)."""
    try:
        with open(state_path()) as fh:
            data = json.load(fh)
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    return {tid for tid, rec in data.items()
            if isinstance(rec, dict) and rec.get("start_sent")
            and not rec.get("end_sent")}


def mark_self_stopped(task_id, now=None):
    """The owner session stopped `task_id` itself (TaskStop confirmed): its
    END is withheld, the rule a self-cancelled dispatch already follows
    (DGN-1688 CANCEL_BY=self).  True when a started job was marked."""
    now = time.time() if now is None else now
    with _Locked() as lk:
        rec = lk.state.get(task_id)
        if not isinstance(rec, dict) or rec.get("end_sent"):
            return False
        rec["self_stopped"] = True
        lk.save(now)
    return True


def record_end(task_id, status, end_epoch=None, now=None, sender=None):
    """Send the END notice for `task_id` once, only after its START was sent.
    `status` is the task notification status (completed / failed / killed);
    a job the session itself stopped (mark_self_stopped) is closed quietly.
    A notice with no status is withheld rather than guessed as a verdict.
    Returns "sent", "quiet", "skip" or "dup"."""
    now = time.time() if now is None else now
    end_epoch = now if end_epoch is None else end_epoch
    sender = sender or _spawn
    with _Locked() as lk:
        rec = lk.state.get(task_id)
        if not isinstance(rec, dict) or not rec.get("start_sent"):
            return "skip"
        if rec.get("end_sent"):
            return "dup"
        rec["end_sent"] = True
        rec["status"] = status or ""
        rec["recorded"] = now
        quiet = not status or (rec.get("self_stopped")
                               and status != "completed")
        if quiet:
            rec["quiet"] = "self-stop" if status else "no-status"
        lk.save(now)
    if quiet:
        _log("end notice withheld for %s: %s" % (task_id, rec["quiet"]))
        return "quiet"
    slot = rec.get("slot") or model_slot(rec.get("tool"))
    argv = ["--end-notice", "--display-name", _unlabelled(rec["name"], slot),
            "--model-slot", slot,
            "--result", status,
            "--end-clock", time.strftime("%H:%M", time.localtime(end_epoch))]
    # The START waits out the dispatch join window in the flusher; a job
    # that ends inside that window must not have its END arrive first.
    not_before = float(rec.get("queued") or 0) + start_window() + 2
    if not_before > now:
        argv += ["--not-before", str(int(not_before))]
    try:
        elapsed = int(end_epoch - float(rec["start"]))
        if elapsed >= 0:
            argv += ["--elapsed-secs", str(elapsed)]
    except (KeyError, TypeError, ValueError):
        pass
    sender(argv)
    return "sent"


def sync_from_scan(open_jobs, closed, catchup_secs, now=None, sender=None):
    """Stop-hook side.  open_jobs: task id -> (idx, name, start, tool);
    closed: task id -> (status, end_epoch).  Fresh open jobs whose START the
    PostToolUse hook missed are caught up (launch younger than catchup_secs,
    so old history never floods the owner); every closure of a started job
    gets its END.  The whole transcript is re-scanned on every Stop, so the
    state is read once to pick the few ids that still need a notice."""
    now = time.time() if now is None else now
    with _Locked() as lk:
        known = dict(lk.state)
    for tid, job in sorted(open_jobs.items(), key=lambda kv: kv[1][0]):
        _, name, started, tool = job[:4]
        slot = job[4] if len(job) > 4 else None
        name = name or slot or tool  # scan leaves a missing description empty
        if (tid not in known and started is not None
                and now - started < catchup_secs):
            record_start(tid, tool, name, started, now=now, sender=sender,
                         slot=slot)
    for tid, (status, end_epoch) in closed.items():
        rec = known.get(tid)
        if isinstance(rec, dict) and rec.get("start_sent") \
                and not rec.get("end_sent"):
            record_end(tid, status, end_epoch, now=now, sender=sender)
