"""Stop text-layer seat classifier (v2.7 D2, dec-328b).

Harness-agnostic: no transcript I/O here.  The adapter
(routines/text-seat-stop.py) builds a Turn from the transcript and asks
classify_all() for the turn's findings; it logs them to the canary and, in
block mode, blocks once with the first block-eligible one.  Spec, per-row
evidence and FP numbers: docs/v27-D2-spec.md.

Design rules (spec section 1):
  - One block per turn, never a list: rows are tried in ROW_ORDER and the
    first block-eligible hit wins.
  - Each row needs a TEXT signal AND (where the row says so) the absence of
    TRANSCRIPT evidence in this turn.  Evidence that cannot be read (tools
    is None) never produces a finding.
  - Mention forms never fire: fenced code (except for HF30, which is about
    fences), blockquotes, inline code, quoted spans.
  - Framework vocabulary only, and no language words in this file: the
    language vocabulary lives in text_seat_vocab/<lang>.tsv (data, every
    file always loaded).  Instance words (the user's form of address, names
    of the agent's own sessions) arrive through Config.
  - What a row reads: V01/V03/HA13/HF30/HF27/HF48b the final message only
    (the keyboard is built from the final reply); HF52 and HF48 also read
    the turn's interim text messages (dec-328 addendum: Stop seats read
    narration-tagged blocks too).
"""
import os
import re
import sys
from typing import List, Optional, Sequence

_LIB = os.path.dirname(os.path.abspath(__file__))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


class Tool:
    """One tool_use of the turn: name + the fields the rows read."""

    __slots__ = ("name", "command", "path", "bg")

    def __init__(self, name, command=None, path=None, bg=False):
        self.name = str(name or "")
        self.command = command if isinstance(command, str) else ""
        self.path = path if isinstance(path, str) else ""
        self.bg = bool(bg)


class Turn:
    """Everything a row may read.

    reply    -- text of the final assistant message of the turn
    prompt   -- text of the genuine user prompt that opened the turn ("" when
                unknown)
    tools    -- list of Tool for the current turn, or None when the adapter
                could not read the turn (rows needing evidence then stay off)
    injected -- the turn was opened by a machine-injected prompt (bridge
                [..] header, cron inject), not typed by the user
    history  -- up to HISTORY_PROMPTS earlier user prompts of the session,
                newest first (register guard for HF48b)
    interim  -- ((message id, text), ...) of the turn's earlier text
                messages, oldest first (HF52 / HF48 read them)
    reply_mid -- message id of the final message
    """

    def __init__(self, reply, prompt="", tools=None, injected=False,
                 history=(), interim=(), reply_mid=None):
        self.history = tuple(history or ())
        self.reply = reply or ""
        self.prompt = prompt or ""
        self.tools = tools
        self.injected = bool(injected)
        self.interim = tuple(interim or ())
        self.reply_mid = reply_mid

    def texts(self):
        """((mid, text), ...) of every text message, the final one last."""
        return self.interim + ((self.reply_mid, self.reply),)


HISTORY_PROMPTS = 5


class Config:
    """Instance knobs (spec section 1.6).  Defaults = template.

    rows / detect_rows: None -> the template default; an empty sequence ->
    no row (an empty knob really disables)."""

    def __init__(self, rows=None, owner_terms=(), self_session_terms=(),
                 auth_commands=(), decision_ledger=(), technical_user=False,
                 detect_rows=None):
        self.rows = tuple(rows) if rows is not None else DEFAULT_ROWS
        self.detect_rows = tuple(detect_rows) if detect_rows is not None \
            else DEFAULT_DETECT_ROWS
        self.owner_terms = tuple(t for t in owner_terms if t)
        self.self_session_terms = tuple(t for t in self_session_terms if t)
        self.auth_commands = tuple(auth_commands) or DEFAULT_AUTH_COMMANDS
        self.decision_ledger = tuple(p for p in decision_ledger if p)
        self.technical_user = bool(technical_user)


class Finding:
    """One row verdict.  guard is None for a hit; for a first-regex hit a
    false-positive guard suppressed, it names that guard (canary FP
    baseline, never blockable).  mid = the message the signal sits in
    (None -> the final message)."""

    __slots__ = ("row", "excerpt", "message", "mid", "guard")

    def __init__(self, row, excerpt, message, mid=None, guard=None):
        self.row = row
        self.excerpt = excerpt
        self.message = message
        self.mid = mid
        self.guard = guard

    def __repr__(self):
        return "Finding(%s, %r%s)" % (self.row, self.excerpt[:60],
                                      ", guard=%s" % self.guard
                                      if self.guard else "")


# ---------------------------------------------------------------------------
# Language vocabulary (text_seat_vocab/<lang>.tsv)
# ---------------------------------------------------------------------------
VOCAB_DIR = os.path.join(_LIB, "text_seat_vocab")
VOCAB_LANGS = ("ko", "en")
# Locales whose vocabulary was measured on a corpus.  Any other locale runs
# every row detect-only (spec 1.3); G03 is ko-only (spec 4).
MEASURED_LANGS = ("ko",)
# An ASCII word edge.  Python's \b treats Hangul as a word character, so
# "push" in "push<Hangul particle>" would never match a \b-anchored word.
_W = "A-Za-z0-9_"
_ASCII_B = r"(?:(?<![%s])(?=[%s])|(?<=[%s])(?![%s]))" % (_W, _W, _W, _W)
_NEVER = r"(?!x)x"
VOCAB_ERRORS = []  # [(lang, reason)] -- the adapter logs them as SKIP


def load_vocab(langs=VOCAB_LANGS, vocab_dir=VOCAB_DIR, errors=None):
    """{NAME: [pattern, ...]} over every language file."""
    out = {}
    for lang in langs:
        path = os.path.join(vocab_dir, lang + ".tsv")
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError as e:
            if errors is not None:
                errors.append((lang, "unreadable: %s" % e.__class__.__name__))
            continue
        for n, line in enumerate(lines, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            name, sep, pat = line.partition("\t")
            pat = re.sub(r"(?<!\\)\\b", lambda _m: _ASCII_B, pat.strip())
            try:
                re.compile(pat)
            except re.error:
                if errors is not None:
                    errors.append((lang, "bad pattern line %d" % n))
                continue
            if sep and name.strip() and pat:
                out.setdefault(name.strip(), []).append(pat)
    return out


_VOCAB = load_vocab(errors=VOCAB_ERRORS)


def _alt(name, extra=()):
    parts = list(extra) + _VOCAB.get(name, [])
    return "|".join("(?:%s)" % p for p in parts) or _NEVER


def _vrx(name, flags=re.I, extra=()):
    return re.compile(_alt(name, extra), flags)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
_FENCE_BLOCK_RE = re.compile(r"^[ \t]*(```|~~~)([^\n]*)\n(.*?)(?:^[ \t]*\1[ \t]*$|\Z)",
                             re.DOTALL | re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# Quoted spans: ASCII, curly and corner-bracket quotes.
_QUOTED_RE = re.compile('"[^"\\n]{1,200}"|\u201c[^\u201d\\n]{1,200}\u201d|'
                        '\u300c[^\u300d\\n]{1,200}\u300d|'
                        '\u2018[^\u2019\\n]{1,200}\u2019')
_MARKER_LINE_RE = re.compile(r"^\s*(\[\[|[a-z_]+::)")
_SENT_SPLIT_RE = re.compile("(?<=[.!?\u3002\uff1f])\\s+|\\n")
_NUM_LINE_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+(\S.*)$")
_QMARK = "\\?|\uff1f"


def fences(text):
    """[(info string, body)] for every fenced block."""
    return [(m.group(2).strip().lower(), m.group(3))
            for m in _FENCE_BLOCK_RE.finditer(text or "")]


def prose(text, keep_inline_code=False):
    """Visible prose lines: no fences, blockquotes, marker lines, tables;
    inline code and quoted spans blanked (they are mentions)."""
    body = _FENCE_BLOCK_RE.sub("\n", text or "")
    out = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith(">") or s.startswith("|"):
            continue
        if _MARKER_LINE_RE.match(ln):
            continue
        if not keep_inline_code:
            ln = _INLINE_CODE_RE.sub(" ", ln)
        ln = _QUOTED_RE.sub(" ", ln)
        out.append(ln.replace("**", "").replace("__", ""))
    return out


def sentences(lines):
    out = []
    for ln in lines:
        out.extend(s.strip() for s in _SENT_SPLIT_RE.split(ln) if s and s.strip())
    return out


def options_labels(text):
    """Button labels of the reply's decision menu, else None.

    The marker is found by the hook side's one marker parser,
    dispatch_return_forcing.options_marker_lines (bridge mirror: standalone
    line, bare or labeled, outside fenced code).  Labeled marker -> its
    labels; bare marker -> the last numbered run in prose."""
    try:
        import dispatch_return_forcing as F
    except Exception:
        return None
    lines = F.options_marker_lines(text or "")
    if not lines:
        return None
    labels = F.options_marker_labels(lines[-1])
    if labels is not None:
        return labels
    run, cur = [], []
    for ln in _FENCE_BLOCK_RE.sub("\n", text).splitlines():
        s = ln.lstrip("> ").rstrip()
        mm = _NUM_LINE_RE.match(s)
        if not mm:
            continue
        n = int(mm.group(1))
        if n == 1:
            if len(cur) >= 2:
                run = cur
            cur = [mm.group(2)]
        elif cur and n == len(cur) + 1:
            cur.append(mm.group(2))
    if len(cur) >= 2:
        run = cur
    return run


def no_push(text):
    """The reply carries the bridge's NO_PUSH delivery sentinel
    (bridge/machine_gate.strip_no_push_sentinel, DGN-1732): the bridge
    delivers nothing of this turn to the user.  False when the recognizer
    cannot be loaded (the row then runs)."""
    try:
        root = os.path.dirname(os.path.dirname(_LIB))
        if root not in sys.path:
            sys.path.insert(0, root)
        from bridge.machine_gate import strip_no_push_sentinel
        return bool(strip_no_push_sentinel(text or "")[1])
    except Exception:
        return False


def _excerpt(s, n=140):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


# ---------------------------------------------------------------------------
# Evidence helpers (transcript side)
# ---------------------------------------------------------------------------
_SEARCH_TOOL_NAMES = ("Grep", "Glob", "Read", "WebSearch", "WebFetch",
                      "NotebookRead", "ToolSearch", "Task", "Agent")
_SEARCH_TOOL_NAME_RE = re.compile(
    r"(search|fetch|read|query|list|get|find|recall|memory)", re.I)
_SEARCH_CMD_RE = re.compile(
    r"(\b(grep|rg|ag|find|ls|cat|head|tail|sed|awk|jq|sqlite3|less|wc|"
    r"mdfind|locate|tree|stat|curl)\b|\bgit\s+(log|grep|show|diff|status|"
    r"branch)\b|search|recall|memory|ledger|lifekit|\.sh\b|\.py\b)", re.I)


def searched(tools):
    """True when any tool_use of the turn could have looked something up.
    Generous on purpose: over-crediting costs recall, never precision."""
    for t in tools:
        if t.name in _SEARCH_TOOL_NAMES:
            return True
        if t.name.startswith("mcp__") and _SEARCH_TOOL_NAME_RE.search(t.name):
            return True
        if t.name == "Bash" and _SEARCH_CMD_RE.search(t.command):
            return True
    return False


def touched(tools, globs):
    """True when a Write/Edit path or a Bash command names any of `globs`
    (plain substrings; the knob holds paths like 'worklog/_DECISIONS.md')."""
    for t in tools:
        blob = t.path if t.name != "Bash" else t.command
        if any(g in blob for g in globs):
            return True
    return False


# ---------------------------------------------------------------------------
# Rows.  Each returns Optional[Finding].
# Block copy is MODEL-facing (hook feedback), English, one rule, one pointer.
# ---------------------------------------------------------------------------
TEXT_DOC = "rules/cold.framework.text.md"
WHY_DOC = "rules/cold.framework.why.md"
# The Options judgment rules live in the channel contract today (the vendor
# file the session spawner injects); spec open question Q2 proposes moving
# them into TEXT_DOC before any V01/V03 block.
OPTIONS_DOC = "the channel contract (vendors/<channel>.md)"
OPTIONS_SECTION = "Options: judgment rules"
_TAIL = " This seat blocks once per turn; rewrite the reply, do not argue."

# --- HF27: absence claim with no search this turn ------------------------- #
# The claim is about RECORDED knowledge (a record, a note, a memory, a
# trace) or the agent's own not-knowing.  A world-state negative ("no
# errors", "nothing changed") is not an absence-of-knowledge claim.
_ABSENCE_RE = _vrx("ABSENCE")
# The claim answers a RECALL question: the user asked about a past fact,
# record or thing they said.  Without it, "I don't know" is honest
# uncertainty about the future or the user's intent (measured: 7 of 7
# unguarded fires were that) -- the behaviour the rules want.
_RECALL_Q_RE = _vrx("RECALL_Q")
_QUESTION_END_RE = re.compile(r"(?:%s)\s*$" % _alt("QUESTION_END", (_QMARK,)))


def row_hf27(turn, cfg):
    """First regex = an absence sentence.  Guards (logged when they
    suppress): `searched` (the turn looked it up) and `recall_q` (the
    prompt is not a recall question)."""
    if turn.tools is None:
        return None
    for s in sentences(prose(turn.reply)):
        if _QUESTION_END_RE.search(s):
            continue
        if not _ABSENCE_RE.search(s):
            continue
        guard = ("searched" if searched(turn.tools)
                 else None if _RECALL_Q_RE.search(turn.prompt) else "recall_q")
        return Finding("HF27", _excerpt(s), (
            "HF27 absence claim with no search (text seat, v2.7 D2). "
            "Your reply says something is not recorded / not known -- "
            "%r -- but no tool in this turn looked it up. Search first "
            "(memory search, the files that would hold it, the "
            "structured store), then answer with what you found or name "
            "where you looked. Rule: %s -- Solve-first.%s"
            % (_excerpt(s, 100), WHY_DOC, _TAIL)), guard=guard)
    return None


# --- HF30: shell command handed to the user ------------------------------ #
_SHELL_INFO = ("bash", "sh", "zsh", "shell", "console", "terminal", "fish",
               "powershell", "ps1", "cmd")
_SHELL_LINE_RE = re.compile(
    r"^\s*(\$\s+|sudo\s|cd\s|git\s|npm\s|npx\s|pnpm\s|yarn\s|pip3?\s|"
    r"brew\s|python3?\s|bash\s|sh\s|zsh\s|chmod\s|chown\s|rm\s|mv\s|cp\s|"
    r"mkdir\s|kill(all)?\s|launchctl\s|systemctl\s|curl\s|wget\s|"
    r"export\s|source\s|\./|~/|open\s|defaults\s|xcode-select\s|"
    r"docker\s|claude\s|gh\s|uv\s|cargo\s|go\s|make\b)")
# The reply hands the run to the user: an imperative addressed to them.
_HANDOVER_RE = _vrx("HANDOVER")
# Commands that need the user's own credential: the one sanctioned handover
# (HF33).  Matched on the fence body.  Instance knob extends it.
DEFAULT_AUTH_COMMANDS = (
    r"\bclaude\s+(/?login|setup-token|auth)\b", r"\bgh\s+auth\b",
    r"\bgcloud\s+auth\b", r"\bcodex\s+login\b", r"\bop\s+signin\b",
    r"\baz\s+login\b", r"\baws\s+(configure|sso\s+login)\b",
    r"\bfirebase\s+login\b", r"\bnpm\s+login\b", r"\bdocker\s+login\b",
    r"\bssh-copy-id\b", r"\bsudo\b", r"\bpasswd\b", r"\bsecurity\s+"
    r"(add|unlock)-", r"\b(bot_?token|BotFather)\b",
)
# The fence is a relay for another agent / person, not a run for the user
# (dec-328: a relay is ONE fenced block).
_RELAY_RE = _vrx("RELAY")
_ASK_FOR_CMD_RE = _vrx("ASK_FOR_CMD")
# The run happens where the agent cannot act: another OS account or host
# (ssh, a fresh account the user just created, a user switch) or an
# interactive prompt that only the person at the keyboard can answer.
_ELSEWHERE_RE = _vrx("ELSEWHERE", extra=(r"\bssh\b", r"\bsu\s+-"))


def _is_shell_fence(info, body):
    if info.split()[:1] and info.split()[0] in _SHELL_INFO:
        return True
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines or len(lines) > 12:
        return False
    hits = sum(1 for ln in lines if _SHELL_LINE_RE.match(ln))
    return hits >= max(1, (len(lines) + 1) // 2)


def row_hf30(turn, cfg):
    """First regex = a shell fence + handover prose.  Guards (logged when
    they suppress): ask_for_cmd, relay, elsewhere, auth."""
    shell = [(i, b) for i, b in fences(turn.reply) if _is_shell_fence(i, b)]
    if not shell:
        return None
    body_prose = "\n".join(prose(turn.reply, keep_inline_code=True))
    if not _HANDOVER_RE.search(body_prose):
        return None
    auth = [re.compile(p, re.I) for p in cfg.auth_commands]
    guard = None
    if _ASK_FOR_CMD_RE.search(turn.prompt):
        guard = "ask_for_cmd"  # the user asked for the command itself
    elif _RELAY_RE.search(body_prose):
        guard = "relay"
    elif _ELSEWHERE_RE.search(body_prose) or any(
            _ELSEWHERE_RE.search(b) for _i, b in shell):
        guard = "elsewhere"  # only the user can run it there
    pick = None
    for _info, body in shell:
        if not any(a.search(body) for a in auth):
            pick = body
            break
    if pick is None:
        guard, pick = guard or "auth", shell[0][1]
    cmd = _excerpt((pick.strip().splitlines() or [""])[0], 80)
    return Finding("HF30", cmd, (
        "HF30 command handed to the user (text seat, v2.7 D2). Your "
        "reply asks the user to run %r themselves. Run it yourself with a "
        "tool and report the result; hand over a command only when it "
        "needs the user's own credential (login, OAuth, password). "
        "Rule: %s -- Internal bold, external careful (run commands "
        "yourself).%s" % (cmd, WHY_DOC, _TAIL)), guard=guard)


# --- HF48: internal mechanics in user-facing prose ------------------------ #
# JARGON (output_gate._check_jargon) was disabled 2026-08-13: it counted ONE
# path/func token anywhere.  Tier b needs density (>= 3 distinct internal
# tokens in prose, code/quotes/inline-code excluded), a non-technical user
# (the prompt carries no such token and the instance knob does not mark the
# user technical) and a non-failure reply.
_A0 = r"(?<![A-Za-z0-9_])"
_A1 = r"(?![A-Za-z0-9_])"
_MECH_RES = (
    re.compile(r"(?<![\w/.])(?:~|/(?:Users|home|tmp|private|var|opt|etc))"
               r"/[\w./\-]+"),                                   # abs path
    re.compile(_A0 + r"[\w\-]+\.(?:sh|py|jsonl?|ya?ml|toml|plist|conf|db|"
               r"sqlite)" + _A1),                                # script/file
    re.compile(_A0 + r"[a-z][a-z0-9]{2,}(?:_[a-z0-9]+)+" + _A1),  # snake_case
    re.compile(_A0 + r"[a-z][a-z0-9]+(?:[A-Z][a-z0-9]+)+" + _A1),  # camelCase
    re.compile(r"(?<![\w-])--[a-z][\w\-]{2,}"),                  # --flag
    re.compile(_A0 + r"[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+" + _A1),  # ENV_VAR
    re.compile(_A0 + r"(?:PostToolUse|PreToolUse|UserPromptSubmit|tool_use|"
               r"tool_result|transcript|stdin|stdout|subagent|"
               r"settings\.json|frontmatter|crontab|launchd|worktree)" + _A1),
)
_FAILURE_RE = _vrx("FAILURE")
MECH_MIN_TOKENS = 3
# Tier a: the harness's own control text reached the reply (a context dump:
# token budget lines, hook context headers, reminder tags, tool ids).  No
# user ever needs these, so no register guard applies.  Claude Code harness
# strings only -- framework- and instance-neutral.  The bridge render step
# is the root home for this tier (grill F11, separate change); this row is
# its detect cross-check.
_HARNESS_LEAK_RE = re.compile(
    r"(<total_tokens>|</?system-reminder>|hook additional context:|"
    r"<command-(?:name|message)>|"
    r"\btoolu_[A-Za-z0-9]{8,}|\btool_use_id\b|<function_calls>|"
    r"<invoke name=)")


def mechanics_tokens(text):
    found = []
    for ln in prose(text):
        for rx in _MECH_RES:
            for m in rx.finditer(ln):
                tok = m.group(0)
                if tok not in found:
                    found.append(tok)
    return found


def row_hf48(turn, cfg):
    """Tier a: harness control text in any text message of the turn,
    outside fenced and inline code (a mention of a tag in backticks is
    talk about the tag, grill F12).  No register guard."""
    for mid, text in turn.texts():
        body = _INLINE_CODE_RE.sub(" ", _FENCE_BLOCK_RE.sub("\n", text or ""))
        m = _HARNESS_LEAK_RE.search(body)
        if m:
            return Finding("HF48", m.group(0), (
                "HF48 internal context leaked into the reply (text seat, v2.7 "
                "D2): the reply carries harness control text (%r) -- an "
                "injected context block, not something you meant to say. "
                "Rewrite the reply with only the answer for the user. Rule: "
                "%s -- Register.%s" % (m.group(0), TEXT_DOC, _TAIL)), mid=mid)
    return None


def row_hf48b(turn, cfg):
    """Tier b: plumbing density, only for a non-technical register -- this
    prompt AND the recent ones carry no internal tokens and the instance
    does not mark the user technical.  Detect-only (spec: 0 true / 3 false
    on the corpus)."""
    if cfg.technical_user:
        return None
    if any(mechanics_tokens(p) for p in [turn.prompt] + list(turn.history)):
        return None  # the user speaks this register
    toks = mechanics_tokens(turn.reply)
    if len(toks) < MECH_MIN_TOKENS:
        return None
    if _FAILURE_RE.search("\n".join(prose(turn.reply))):
        return None  # failure report: internals may be needed
    shown = ", ".join(toks[:4])
    return Finding("HF48b", shown, (
        "HF48 internal mechanics in the reply (text seat, v2.7 D2). The "
        "reply shows the user internal plumbing (%s). Describe outcomes in "
        "the user's own terms ('marked 7 tasks done', not the script or file "
        "that did it); internals appear only for a failure that needs them "
        "or when the user asked. Rule: %s -- Register.%s"
        % (shown, TEXT_DOC, _TAIL)))


# --- HF52: one self across sessions -------------------------------------- #
# Generic forms: a demonstrative + "session" used as a third party that DID
# or KNOWS something.  Instance words for its own parallel sessions come in
# through cfg.self_session_terms.
_OTHER_SESSION_RE = _vrx("OTHER_SESSION")
# "Session" in its OS / network sense (a GUI login, a terminal, ssh) is not
# the agent's own session.
_OS_SESSION_RE = _vrx("OS_SESSION")


def row_hf52(turn, cfg):
    """Reads every text message of the turn (interim narration too).  A
    turn whose final reply carries NO_PUSH reaches nobody: skipped."""
    if no_push(turn.reply):
        return None
    terms = [re.escape(t) for t in cfg.self_session_terms]
    extra = re.compile("(%s)" % "|".join(terms)) if terms else None
    for mid, text in turn.texts():
        for s in sentences(prose(text)):
            m = _OTHER_SESSION_RE.search(s) or (extra.search(s) if extra
                                                else None)
            if not m or _OS_SESSION_RE.search(s):
                continue
            return Finding("HF52", _excerpt(s), (
                "HF52 one self (text seat, v2.7 D2). The reply speaks of "
                "your own parallel or earlier session as someone else -- %r. "
                "You are one continuous self: say what you did / are doing "
                "('I am also working on X', 'earlier I found Y'). "
                "Rule: %s -- Register (one self).%s"
                % (_excerpt(s, 100), TEXT_DOC, _TAIL)), mid=mid)
    return None


# --- V01 / TP02 / HA12: execution-order menus ------------------------------ #
# A go/hold menu after the user said go is NOT this row: the DGN-1940
# dispatch-return forcing seat owns that shape (grill answer 5).
# An order label: "<task> first".  Time words ("from now", "from the
# start") and the priority word are not order labels (vocab ORDER_LABEL).
_ORDER_LABEL_RE = _vrx("ORDER_LABEL")
# The user is the actor of the question: the menu picks the user's own
# activity or direction -- their What, a real decision.
_USER_ACTOR_RE = _vrx("USER_ACTOR")
# An order question put to the user: a question sentence that asks WHICH
# goes first / in what order.  A body that merely mentions an order ("the
# next step is ...") is narration, not an order menu.
_ORDER_Q_RE = _vrx("ORDER_Q")
_Q_SENT_RE = re.compile(r"(?:%s)\s*\S{0,3}$" % _alt("Q_SENT", (_QMARK,)))
# Consequential content: the menu may be a real decision (irreversible,
# external, costly, the user's own life/data).  Any hit -> stay silent.
# English words are whole words ("staging" is not "tag", grill F10).
_CONSEQUENCE_RE = _vrx("CONSEQUENCE")


def _short_label(label):
    """Label head: before an em dash / colon / paren gloss, no emphasis."""
    head = re.split("\\s[\u2014\\-:]\\s|\\s?\\(|:", label.replace("**", ""),
                    maxsplit=1)[0]
    return head.strip()


def row_v01(turn, cfg):
    labels = options_labels(turn.reply)
    if not labels or len(labels) < 2:
        return None
    body = "\n".join(prose(turn.reply))
    qs = [q for q in sentences(prose(turn.reply)) if _Q_SENT_RE.search(q)]
    if any(_USER_ACTOR_RE.search(q) for q in qs):
        return None  # the user picks their own activity: a real decision
    short = [_short_label(lb) for lb in labels]
    order = (sum(1 for lb in short if _ORDER_LABEL_RE.search(lb)) >= 2
             or any(_ORDER_Q_RE.search(q) for q in qs))
    if not order:
        return None
    if _CONSEQUENCE_RE.search(body + " " + " ".join(labels)):
        return None
    shown = " | ".join(labels)
    return Finding("V01", _excerpt(shown), (
        "V01 options on a non-decision (text seat, v2.7 D2): execution-order "
        "menu -- [%s]. "
        "Order and continuation are yours to carry: do the work (in parallel "
        "where independent) and report; put a menu only on a real decision "
        "(the user's Why/What, irreversible, external or costly). "
        "Rule: %s -- %s.%s" % (_excerpt(shown, 100), OPTIONS_DOC,
                               OPTIONS_SECTION, _TAIL)))


# --- V03: option reference scheme ---------------------------------------- #
_ALT_SCHEME_RE = _vrx("ALT_SCHEME", extra=(r"(?<![\w(])\([a-eA-E]\)",
                                           "[\u2460-\u2464]"))
# The alternate token must sit in a CHOICE sentence: a question to the user
# or a recommendation naming it.  Circled numbers and (a)/(b) also label
# sample parts, settings or findings -- those are not option references.
_ASK_END_RE = re.compile(r"(?:%s)\s*[*_)]*\s*$" % _alt("ASK_END", (_QMARK,)))
_REC_NEAR_RE = re.compile(r"(?:%s)\s*\**\s*$" % _alt("REC_NEAR"), re.I)


def row_v03(turn, cfg):
    labels = options_labels(turn.reply)
    if not labels:
        return None
    for s in sentences(prose(turn.reply)):
        m = _ALT_SCHEME_RE.search(s)
        if not m:
            continue
        if not (_ASK_END_RE.search(s)
                or _REC_NEAR_RE.search(s[:m.start()])):
            continue
        tok = m.group(0)
        if any(tok in lb for lb in labels):
            continue  # the scheme is the label text itself
        return Finding("V03", _excerpt(s), (
            "V03 option reference scheme (text seat, v2.7 D2). The reply "
            "refers to its options as %r while the buttons carry different "
            "text. Refer to options by their list number (1/2/3) while the "
            "numbered list is in the body, otherwise by label; never a "
            "separate (a)/(b) or A/B scheme. Rule: %s -- %s.%s"
            % (tok, OPTIONS_DOC, OPTIONS_SECTION, _TAIL)))
    return None


# --- HA13: decision ask with no ledger edit (instance opt-in) ------------- #
def row_ha13(turn, cfg):
    if not cfg.decision_ledger or turn.tools is None:
        return None
    labels = options_labels(turn.reply)
    if not labels or len(labels) < 2:
        return None
    if touched(turn.tools, cfg.decision_ledger):
        return None
    return Finding("HA13", " | ".join(labels)[:140], (
        "HA13 decision ask with no ledger line (text seat, v2.7 D2). The "
        "reply asks the user to decide, but this turn did not write the "
        "pending decision to %s. Add the ledger line (and its decision file) "
        "in this turn, then send the ask. Rule: this instance's decision "
        "ledger rule.%s" % (", ".join(cfg.decision_ledger), _TAIL)))


# --- DN04: done claim without an empty open-items line (DGN-2048) -------- #
# Canon (rules/hot.framework.md -- Done): every completion report opens with
# what does not work yet, and done may be said only when that list is empty.
# Fires on an assertive done claim in the final reply's prose when the reply
# has no open-items line, or has one that lists something.  Record-first:
# a detect row in the template (DEFAULT_DETECT_ROWS); block promotion waits
# for the canary count, like every other row.
HOT_DOC = "rules/hot.framework.md"
_DONE_CLAIM_RE = _vrx("DONE_CLAIM")
_OPEN_ITEMS_RE = re.compile(r"^(?:%s)" % _alt("OPEN_ITEMS"), re.I)
_OPEN_NONE_RE = _vrx("OPEN_NONE")
_LEAD_MARK_RE = re.compile(r"^(?:[#>*+\-\s]|\d{1,2}[.)]\s|\*\*|__)+")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+\u2022]|\d{1,2}[.)])\s+(\S.*)$")
_HEAD_SEP_RE = re.compile(r"^[^:\uff1a\n]{0,40}?[:\uff1a]\s*|"
                          r"^[^\n]{0,40}?\s[\u2014\-]+\s+")


def _bare(line):
    return _LEAD_MARK_RE.sub("", line).replace("**", "").replace("__", "") \
        .strip()


def open_items_state(text):
    """'missing' (no open-items line), 'empty' (the line says none, or
    lists nothing) or 'open' (it lists at least one item).  Fenced code
    never counts; a quoted line (blockquote) does -- it is still the
    reply's own layout."""
    lines = _FENCE_BLOCK_RE.sub("\n", text or "").splitlines()
    for i, ln in enumerate(lines):
        head = _bare(ln)
        m = _OPEN_ITEMS_RE.match(head)
        if not m:
            continue
        rest = head[m.end():]
        sep = _HEAD_SEP_RE.match(rest)
        value = (rest[sep.end():] if sep else rest).strip()
        items = [value] if value else []
        if not items:
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    if items:
                        break
                    continue
                li = _LIST_ITEM_RE.match(nxt.strip().lstrip(">").strip())
                if not li:
                    break
                items.append(li.group(1).strip())
        items = [x for x in (_bare(x) for x in items) if x]
        if all(_OPEN_NONE_RE.search(x) for x in items):
            return "empty"
        return "open"
    return "missing"


def row_dn04(turn, cfg):
    """First regex = an assertive done claim (questions are not claims).
    Clear = an open-items line whose list is empty."""
    if no_push(turn.reply):
        return None
    claim = None
    for s in sentences(prose(turn.reply)):
        if _QUESTION_END_RE.search(s):
            continue
        if _DONE_CLAIM_RE.search(s):
            claim = s
            break
    if claim is None:
        return None
    state = open_items_state(turn.reply)
    if state == "empty":
        return None
    why = ("has no open-items line" if state == "missing"
           else "lists open items in the same reply")
    return Finding("DN04", _excerpt(claim), (
        "DN04 done claim (text seat, DGN-2048). The reply says the work is "
        "done -- %r -- but %s. DONE is the user's unit of work with nothing "
        "that worked before broken. Open the report with what does not work "
        "yet; say done only when that list is empty, otherwise call the "
        "slice unfinished. Rule: %s -- Done.%s"
        % (_excerpt(claim, 100), why, HOT_DOC, _TAIL)))


ROWS = {
    "DN04": row_dn04,
    "HF30": row_hf30,
    "HF27": row_hf27,
    "V01": row_v01,
    "V03": row_v03,
    "HA13": row_ha13,
    "HF52": row_hf52,
    "HF48": row_hf48,
    "HF48b": row_hf48b,
}
# Precision-first order: the row with the best measured precision wins a
# tie.  IDLE_PROMISE (G03) is NOT in this table -- the adapter asks it first.
ROW_ORDER = ("HF48", "HF30", "HF27", "V01", "V03", "HA13", "HF52", "HF48b",
             "DN04")
# Template defaults (spec 1.6).  DEFAULT_ROWS = the block CANDIDATES: in
# detect mode their winner is logged would_block=1, which is what the
# block switch (spec section 7) is judged on.  Nothing blocks in the
# template: the wiring pins TEXT_SEAT_MODE=detect.  HF48 stays detect here
# (its root home is the bridge render step, grill F11).  HA13 is in
# neither: it needs an instance ledger path.
DEFAULT_ROWS = ("V01", "V03", "HF52")
# DN04 (DGN-2048) lands record-first: detect, never a block candidate yet.
DEFAULT_DETECT_ROWS = ("HF48", "HF30", "HF27", "HF48b", "DN04")

def classify_all(turn: Turn, cfg: Optional[Config] = None,
                 rows: Optional[Sequence[str]] = None, raw: bool = False,
                 errors: Optional[list] = None) -> List[Finding]:
    """Every enabled row's verdict, in ROW_ORDER (all rows when rows is
    None -- the replay view).  raw=True also returns guard-suppressed
    first-regex hits (Finding.guard set).  A row that raises is skipped and
    its id appended to `errors`."""
    cfg = cfg or Config()
    out = []
    for name in ROW_ORDER:
        if rows is not None and name not in rows:
            continue
        try:
            f = ROWS[name](turn, cfg)
        except Exception:
            f = None  # one broken row never takes the seat down
            if errors is not None:
                errors.append(name)
        if f is not None and (raw or f.guard is None):
            out.append(f)
    return out


def classify(turn: Turn, cfg: Optional[Config] = None,
             rows: Optional[Sequence[str]] = None) -> Optional[Finding]:
    """First hit in ROW_ORDER among the enabled rows (cfg.rows when rows is
    None), else None."""
    cfg = cfg or Config()
    found = classify_all(turn, cfg, rows if rows is not None else cfg.rows)
    return found[0] if found else None
