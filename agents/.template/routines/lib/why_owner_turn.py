"""why_owner_turn -- the owner-turn resolver (spec 2.5, the GRILL point).

resolve(quote, session_id, message_uuid=None) -> TurnRef | Refusal

A quote resolves only when it is a whole-clause substring (after NFC and
whitespace normalization) of a transcript entry the OWNER sent.  Every
check is fail-closed: anything unknown refuses.

Checks (all must pass):
  0. env carries no dispatch-worker marker (spec check 5, V-9 pinned:
     dispatch-detached.sh exports DOGANY_DISPATCH_WORKER at both spawn
     sites, DGN-1878).  A detached worker's user turn is a brief.
  1. runtime is claude-code (CLAUDECODE=1, which Claude Code sets in its
     tool and hook env) and the transcript of `session_id` exists at the ONE
     exact path <projects>/<enc CLAUDE_PROJECT_DIR>/<session_id>.jsonl (no
     search: grill fix 1 -- a search let a forged file in any project dir
     count); otherwise `resolver-unavailable` (a codex confirm fails closed).
  1b. Unless resolving a confirmation, `session_id` is the CURRENT session (the newest owner-turn ledger
     row; a CLAUDE_CODE_SESSION_ID naming another session vetoes): a quote
     from another session's transcript (a worker brief, grill e) never
     resolves.
  2. entry: type=user, message.role=user, userType=external, or an attachment
     carrying a queued_command in prompt mode (userType may be absent).
     No isMeta; content is text (no tool_result block, no toolUseResult). Hardening
     beyond the frozen list (stricter only): isSidechain and
     isCompactSummary entries are refused too.
  3. the turn text carries none of the injection markers (MARKER SETS
     below), imported from their owners where importable.
  3b. sha256(NFC(turn text)) is in the owner-turn LEDGER for that session.
     Only `why-net ups` (the UserPromptSubmit hook) writes the ledger, from
     the real submitted prompt plus a fresh, unused bridge inbound-context
     request_id that is not marked forwarded.  Worker briefs, `claude -p`
     cron prompts, forwarded bubbles and hand-written transcript lines are
     therefore never owner turns (grill fixes 1, 2, 6).
  4. the quote matches after NFC and whitespace normalization AND is a whole
     sentence: it starts and ends at a sentence boundary (turn edge,
     newline or a sentence end -- grill-3 fix 8: ':' '~' ',' and an
     ellipsis are not boundaries).  "X is the goal" cut out of "X is the goal -- not"
     would drop a negation (grill fix 7); such a quote refuses
     `quote-not-clause` and goes through `interpret` instead.
  5. message_uuid, when given, must be the matching entry's uuid.
  4b. (grill-2 fix 5) the quote is not the agent's own line coming back:
     not inside quote markup (a ">" line, a quote pair around it) and not
     found in any assistant reply BEFORE the turn (an owner pasting the
     agent's bubble to reject it); such a quote goes through `interpret`.
     Grill-3 fix 6: also not in a push.sh bubble (the outbound ledger)
     or an earlier session's reply sent before the turn (outside_echo).
  4c. (grill-2 fix 8) the quote is not a question: it does not end in
     "?" / an interrogative ending, and its clause in the turn is not
     followed by "?" (`quote-question`).
  5b. (grill-2 fix 4) the bound ledger row is not source=voice: a Whisper
     transcript is not the owner's typed words.
  4d. (grill-3 fix 7) on a bridge photo / document turn only the caption
     span is the owner's (owner_region); the wrapper text the bridge wrote
     refuses `quote-wrapper`.
  5c. (grill-3 fix 4) the bound ledger row is not source=callback: a tap's
     text is a button label the agent wrote (`quote-tap`); taps confirm
     through the probe path (why_net.events.confirm_tap), or an explicit
     confirmation menu whose exact candidate is re-proven on the chain.
  6. (grill-2 fixes 1-3) the entry sits on the transcript's MAIN CHAIN: the
     parentUuid walk back from the newest entry.  Claude Code writes that
     chain and links each entry to the one it wrote before, never to a line
     an agent's Bash `>>` added, so a hand-appended turn is a dead branch
     (it is already one when the next tool call is logged).  And the entry
     is BOUND to one ledger row: rows and on-chain owner turns pair up in
     order per sha, so a hand-appended ledger row cannot lend a second turn
     a backing.  `audit_ledger()` re-derives every ledger row this way; the
     nightly `derive --audit` flags each one with no real turn behind it.
     Residual (detect-only, spec 9 (i)): a forge and a confirm inside ONE
     Bash call pass the live check while the forged line is still the
     newest entry; the audit catches it once the runtime logs that call.

Pure stdlib.  English/ASCII only.
"""

import collections
import hashlib
import importlib.util
import json
import os
import re
import unicodedata

_HERE = os.path.dirname(os.path.realpath(__file__))
_ROUTINES = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_ROUTINES)

DISPATCH_ENV_MARKER = "DOGANY_DISPATCH_WORKER"
RUNTIME = "claude-code"

# Lockstep literal with bridge/sdk_bridge.py INJECTED_TURN_MARK (DGN-1606).
# The bridge is OSS and not imported here; the floor test pins parity.
BRIDGE_INJECTED_TURN_MARK = "[bridge:injected-turn]"
STOP_HOOK_FEEDBACK = "Stop hook feedback:"
# Stricter-only extras (grill 11c): machine turn heads that live on no owner
# list.  `[dispatch-return]` is the dispatch-detached.sh result turn the
# bridge recognises (sdk_bridge is_dispatch_return); the floor test pins it.
EXTRA_PREFIX_MARKERS = ("[dispatch-return]",)
# Claude Code's own machine heads: the runtime writes them as user entries
# (an interrupt, a slash command or skill load, a reminder, the resume
# caveat).  Formerly owner_turns.MACHINE_PREFIXES, the AC5 reader's second
# list; one list now (DGN-1967 step 3).
RUNTIME_PREFIX_MARKERS = (
    "[Request interrupted",
    "Stop hook feedback",
    "Base directory for this skill",
    "<command-",
    "<system-reminder>",
    "Caveat: The messages below",
)

# Fallback copies, used only when the owner module cannot be imported.  The
# floor test asserts each copy equals its owner's live tuple, so a drift is
# red instead of silent.
_FALLBACK_RECAP_MARKERS = (
    "hook additional context",
    "[session continuity]",
    "hookSpecificOutput",
    "additionalContext",
    "[\uae30\uc5b5\uaddc\uce59]",
)
_FALLBACK_NOISE_MARKERS = (
    "<task-notification",
    "[cron-inject]",
    "[DGN-",
    "Stop hook feedback:",
    "<command-name",
    "<local-command",
    "[\uad00\ub828 \uae30\uc5b5",
    "[\uae30\uc5b5\uaddc\uce59]",
    "[\uad6c\uc870\ud654\uc0c1\ud0dc]",
    "[same-day answer by this agent",
    "[\ud604\uc7ac \uc2e0\uccb4/\ubaa9\ud45c]",
    "[turn frame]",
)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _literal_from_source(path, name):
    """The literal value of the module-level `name = <literal>` in `path`,
    read without executing the module.  memory.py is a 5k-line engine whose
    top-level imports (urllib, sqlite3, ...) cost ~45 ms per hook run; the
    `ups` hook budget is p95 < 150 ms (spec V-11).  Only the one statement
    is parsed; a non-literal value raises (-> fallback copy)."""
    import ast
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    head = name + " ="
    for i, line in enumerate(lines):
        if not line.startswith(head):
            continue
        for j in range(i + 1, min(i + 200, len(lines)) + 1):
            try:
                tree = ast.parse("\n".join(lines[i:j]))
            except SyntaxError:
                continue
            return ast.literal_eval(tree.body[0].value)
    raise LookupError(name)


def _import_markers():
    recap = noise = None
    try:
        recap = tuple(_load_module(
            "_why_recap", os.path.join(_ROUTINES, "session-recap.py")
        )._INJECTION_MARKERS)
    except Exception:
        recap = None
    try:
        noise = tuple(_literal_from_source(
            os.path.join(_ROOT, "memory-engine", "memory.py"),
            "TRANSCRIPT_NOISE_MARKERS"))
    except Exception:
        noise = None
    return recap, noise


_IMPORTED_RECAP, _IMPORTED_NOISE = _import_markers()
# session-recap _INJECTION_MARKERS: substring match.
RECAP_MARKERS = _IMPORTED_RECAP if _IMPORTED_RECAP else _FALLBACK_RECAP_MARKERS
# memory.py TRANSCRIPT_NOISE_MARKERS + bridge mark + Stop feedback: prefix
# match on the lstripped text.
PREFIX_MARKERS = tuple(
    (_IMPORTED_NOISE if _IMPORTED_NOISE else _FALLBACK_NOISE_MARKERS)
) + (BRIDGE_INJECTED_TURN_MARK, "[bridge:", STOP_HOOK_FEEDBACK) + EXTRA_PREFIX_MARKERS \
    + RUNTIME_PREFIX_MARKERS


# Grill-3 fix 7: the bridge wraps a photo / document turn in its own prompt
# text (bridge/i18n photo_prompt_single / photo_prompt_album / doc_prompt +
# path lines); only the owner's caption, after the `user_caption` label, is
# the owner's words.  Read from the bridge's i18n catalogs (plain dicts);
# the fallback copies are pinned to them by the floor test.
WRAPPER_KEYS = ("photo_prompt_single", "photo_prompt_album", "doc_prompt")
CAPTION_KEY = "user_caption"
# The bridge's timeout auto-resume prompt (bot.py): a whole turn the bridge
# wrote, never the owner's.
RESUME_KEY = "resume_continuation_prompt"
_FALLBACK_WRAPPER_STRINGS = {
    "en": {
        "photo_prompt_single":
            "The user sent a photo. Open the image file at the path below with the Read tool, review it, and respond.",
        "photo_prompt_album":
            "The user sent {count} photos at once (an album). Open all image files at the paths below with the Read tool, review them together, and answer with a single response.",
        "doc_prompt":
            "The user sent a file. Open the file at the path below with the Read tool, review it, and respond.",
        "user_caption":
            "User caption: {caption}",
        "resume_continuation_prompt":
            "The previous task was interrupted once by a time limit. Continue from where it stopped. Do not start over; skip what is already done and finish only the remaining work.",
    },
    "ko": {
        "photo_prompt_single":
            "\uc0ac\uc6a9\uc790\uac00 \uc0ac\uc9c4\uc744 \ubcf4\ub0c8\uc2b5\ub2c8\ub2e4. \uc544\ub798 \uacbd\ub85c\uc758 \uc774\ubbf8\uc9c0 \ud30c\uc77c\uc744 Read \ub3c4\uad6c\ub85c \uc5f4\uc5b4\uc11c \ub0b4\uc6a9\uc744 \ud655\uc778\ud558\uace0 \uc751\ub2f5\ud558\uc138\uc694.",
        "photo_prompt_album":
            "\uc0ac\uc6a9\uc790\uac00 \uc0ac\uc9c4 {count}\uc7a5\uc744 \ud55c \ubc88\uc5d0(\uc568\ubc94) \ubcf4\ub0c8\uc2b5\ub2c8\ub2e4. \uc544\ub798 \uacbd\ub85c\uc758 \uc774\ubbf8\uc9c0 \ud30c\uc77c\ub4e4\uc744 \ubaa8\ub450 Read \ub3c4\uad6c\ub85c \uc5f4\uc5b4 \ud568\uaed8 \ubcf4\uace0 \ud558\ub098\uc758 \uc751\ub2f5\uc73c\ub85c \ub2f5\ud558\uc138\uc694.",
        "doc_prompt":
            "\uc0ac\uc6a9\uc790\uac00 \ud30c\uc77c\uc744 \ubcf4\ub0c8\uc2b5\ub2c8\ub2e4. \uc544\ub798 \uacbd\ub85c\uc758 \ud30c\uc77c\uc744 Read \ub3c4\uad6c\ub85c \uc5f4\uc5b4\uc11c \ub0b4\uc6a9\uc744 \ud655\uc778\ud558\uace0 \uc751\ub2f5\ud558\uc138\uc694.",
        "user_caption":
            "\uc0ac\uc6a9\uc790 \ucea1\uc158: {caption}",

        "resume_continuation_prompt":
            "\uc9c1\uc804 \uc791\uc5c5\uc774 \uc2dc\uac04 \uc81c\ud55c\uc73c\ub85c \ud55c \ubc88 \ub04a\uacbc\uc2b5\ub2c8\ub2e4. \ub04a\uae34 \uc9c0\uc810\ubd80\ud130 \uc774\uc5b4\uc11c \uacc4\uc18d \uc9c4\ud589\ud574\uc918. \ucc98\uc74c\ubd80\ud130 \ub2e4\uc2dc \ud558\uc9c0 \ub9d0\uace0, \uc774\ubbf8 \ub05d\ub0b8 \ubd80\ubd84\uc740 \uac74\ub108\ub6f0\uace0 \ub0a8\uc740 \uc791\uc5c5\ub9cc \ub9c8\ubb34\ub9ac\ud574\uc918.",
    },
}


def _import_wrapper_strings():
    out = {}
    for lang in ("en", "ko"):
        try:
            strings = _load_module("_why_i18n_" + lang, os.path.join(
                _ROOT, "bridge", "i18n", lang + ".py")).STRINGS
            out[lang] = {k: strings[k]
                         for k in WRAPPER_KEYS + (CAPTION_KEY, RESUME_KEY)}
        except Exception:
            out[lang] = dict(_FALLBACK_WRAPPER_STRINGS[lang])
    return out


def _wrapper_rules(strings):
    heads, captions = [], []
    for table in strings.values():
        for k in WRAPPER_KEYS:
            parts = [re.escape(unicodedata.normalize("NFC", x))
                     for x in table[k].split("{count}")]
            heads.append(re.compile(r"\d+".join(parts)))
        captions.append(unicodedata.normalize(
            "NFC", table[CAPTION_KEY].split("{caption}")[0]))
    return tuple(heads), tuple(captions)


_WRAPPER_STRINGS = _import_wrapper_strings()
WRAPPER_HEADS, CAPTION_LABELS = _wrapper_rules(_WRAPPER_STRINGS)
RESUME_PROMPTS = tuple(unicodedata.normalize("NFC", t[RESUME_KEY])
                       for t in _WRAPPER_STRINGS.values())


# DGN-1951: the bridge prefixes an owner turn with the messages the agent
# sent outside the session (bridge/outbound_context.py).  Those are the
# agent's words, never the owner's: owner_region starts after the block.
# The ledger reader is the bridge module's too (_outbound_echo below).
def _import_outbound_context():
    try:
        return _load_module("_why_outbound_context", os.path.join(
            _ROOT, "bridge", "outbound_context.py"))
    except Exception:
        return None


OUTBOUND_CONTEXT = _import_outbound_context()


def _after_outbound_block(text):
    # The bridge's own owner_region is the one strip (DGN-1967 step 2).
    # Without the bridge module no block is ever written: nothing to cut.
    if OUTBOUND_CONTEXT is None:
        return 0
    return OUTBOUND_CONTEXT.owner_region(text)[0]


def strip_outbound(text):
    """text without a leading outbound-context block (DGN-1951): the pushes
    the bridge prefixes to an owner turn are the agent's words.  The one
    strip every routines reader takes at its entry point."""
    return text[_after_outbound_block(text):]


def owner_region(text):
    """(start, end) of the part of an NFC owner turn the owner wrote: the
    whole turn, or for a bridge photo / document wrapper only its caption
    (empty when there is none); a leading outbound-context block is never
    the owner's (DGN-1951)."""
    base = _after_outbound_block(text)
    if base:
        lo, hi = owner_region(text[base:])
        return base + lo, base + hi
    if not any(h.match(text) for h in WRAPPER_HEADS):
        return 0, len(text)
    best = None
    for label in CAPTION_LABELS:
        k = text.find("\n" + label)
        if k >= 0 and (best is None or k < best[0]):
            best = (k, k + 1 + len(label))
    return (best[1], len(text)) if best else (len(text), len(text))


def owner_words(text):
    """The owner-authored part of a user message, stripped, or None when
    there is none: the outbound-context block cut, the bridge's auto-resume
    prompt dropped whole, a photo / document wrapper reduced to its caption
    (no caption -> None).  The transcript readers' clean (owner_turns, AC5)."""
    s = strip_outbound((text or "").strip()).strip()
    if not s or s.startswith(RESUME_PROMPTS):
        return None
    lo, hi = owner_region(s)
    return s[lo:hi].strip() or None


# Most specific first: the reason a refused quote reports.
REFUSAL_ORDER = ("quote-tap", "quote-wrapper", "quote-question", "quote-echo", "quote-markup", "quote-voice",
                 "quote-not-clause",
                 "transcript-mismatch",
                 "turn-not-in-ledger",
                 "turn-off-chain")

TurnRef = collections.namedtuple(
    "TurnRef", "runtime session_id message_uuid said_at text")
Refusal = collections.namedtuple("Refusal", "reason")


def _nfc(s):
    return _store().nfc(s)


def quote_key(text):
    """Compare wording with NFC and whitespace folding only, never paraphrase."""
    return " ".join(_nfc(text).split())


def _quote_pattern(quote):
    # Match against the original spacing so clause/markup checks keep newlines.
    return re.compile(r"\s+".join(re.escape(s) for s in quote.split()))


def encode_cwd(cwd):
    # Same encoding as session-recap.py (Claude Code project dir name).
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def projects_dir(env=None):
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def transcript_path(session_id, cwd=None, env=None):
    """The ONE exact transcript path of a session, or None when absent.
    No search across project dirs (grill fix 1)."""
    env = os.environ if env is None else env
    if not session_id or not re.match(r"^[A-Za-z0-9_-]+$", session_id):
        return None
    cwd = cwd or env.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    exact = os.path.join(projects_dir(env), encode_cwd(cwd),
                         session_id + ".jsonl")
    return exact if os.path.isfile(exact) else None


def session_transcript(session_id, cwd=None, env=None):
    """transcript_path(), else the exact path Claude Code handed the hook
    for that session (recorded in the ledger).  Both are exact; there is
    still no search.  The recorded one must name <session_id>.jsonl inside
    THIS agent's project dir (a cron `derive --audit` has no
    CLAUDE_PROJECT_DIR; grill-2: never another project dir, where a whole
    forged session could sit)."""
    env = os.environ if env is None else env
    path = transcript_path(session_id, cwd, env)
    if path or not session_id:
        return path
    base = os.path.realpath(projects_dir(env))
    homes = {os.path.join(base, encode_cwd(d)) for d in
             (cwd, env.get("CLAUDE_PROJECT_DIR"), os.getcwd(), _ROOT) if d}
    for r in reversed(ledger_rows(session_id)):
        p = r.get("transcript_path")
        if isinstance(p, str) and os.path.basename(p) == session_id + ".jsonl" \
                and os.path.dirname(os.path.realpath(p)) in homes \
                and os.path.isfile(p):
            return p
    return None


def turn_sha(text):
    """Ledger key of an owner turn: sha256 of the NFC text."""
    return hashlib.sha256(_nfc(text).encode("utf-8")).hexdigest()


def _store():
    from why_net import store  # lazy: why_net imports this module
    return store


def ledger_rows(session_id=None):
    """Owner-turn rows of the ledger (rows that only consume a request_id
    carry `skip` and are not owner turns)."""
    rows = [r for r in _store().read_rows(_store().TURN_LEDGER)
            if not r.get("skip")]
    if session_id is None:
        return rows
    return [r for r in rows if r.get("session_id") == session_id]


def current_session(env=None):
    """The session a confirm runs in = the session of the newest ledger row
    (the owner's live turn; only the hook writes the ledger).  Claude Code's
    tool env names the session too, but the agent sets the env of its own
    Bash call, so the env can only veto: when it names a different session
    the answer is None (refuse)."""
    env = os.environ if env is None else env
    rows = ledger_rows()
    sid = rows[-1].get("session_id") if rows else None
    named = env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_SESSION_ID")
    if named and named != sid:
        return None
    return sid


# Sentence boundaries for check 4 (grill-3 fix 8).  A quote must be a whole
# sentence: it starts at the turn edge, a newline or after a sentence end,
# and ends on a sentence end, a newline or the turn edge.  ':' (reported
# speech: "the client says: X"), '~', ',' / ';' and an ellipsis ('...',
# '\u2026', "X... is not it") join clauses of ONE sentence, so they are not
# boundaries.
SENTENCE_END = ".!?\u3002\uff01\uff1f"
CLAUSE_PUNCT = SENTENCE_END + ",;:\u2026~\uff0c\u3001\uff1b\uff1a\uff5e"


def _sentence_run(run):
    """True when a punctuation run ends a sentence: sentence enders only,
    and not an ellipsis ('..' or more)."""
    return bool(run) and all(c in SENTENCE_END for c in run) and ".." not in run


def _trailing_punct(s):
    k = len(s)
    while k and s[k - 1] in CLAUSE_PUNCT:
        k -= 1
    return s[k:]


def _leading_punct(s):
    k = 0
    while k < len(s) and s[k] in CLAUSE_PUNCT:
        k += 1
    return s[:k]


# A line ended by ':' introduces what follows ("the client says:\nX",
# "this line is wrong:\nX"): the next line is reported, not said.
INTRO_MARKS = (":", "\uff1a")


def _start_ok(text, i):
    head = text[:i].rstrip(" \t")
    if not head:
        return True
    if head[-1] == "\n":
        return not head.rstrip().endswith(INTRO_MARKS)
    return _sentence_run(_trailing_punct(head))


def _end_ok(text, j, quote):
    own = _trailing_punct(quote)
    if own:
        # the quote's own end punctuation, continued by the turn's ("X." of
        # "X...") is one run
        return _sentence_run(own + _leading_punct(text[j:]))
    tail = text[j:].lstrip(" \t")
    if not tail or tail[0] == "\n":
        return True
    return _sentence_run(_leading_punct(tail))


def span_verdict(text, quote):
    """None when some occurrence of quote in text spans whole clauses outside
    quote markup; else the refusal reason."""
    reason, i = "quote-not-clause", text.find(quote)
    while i >= 0:
        j = i + len(quote)
        if in_quote_markup(text, i, j):
            reason = "quote-markup"  # someone else's words, clause or not
        elif _start_ok(text, i) and _end_ok(text, j, quote):
            return None
        i = text.find(quote, i + 1)
    return reason


def entry_text(entry):
    """The owner text of a transcript entry, or None when the entry is not a
    plain external user text turn (checks 2)."""
    if not isinstance(entry, dict):
        return None
    if entry.get("isMeta") or entry.get("isSidechain") \
            or entry.get("isCompactSummary"):
        return None
    if "toolUseResult" in entry:
        return None
    if entry.get("type") == "attachment":
        # Claude Code delivers real mid-turn input in this structured seat.
        # Rendered system-reminder text is never an owner-message source.
        a = entry.get("attachment")
        if (entry.get("userType", "external") == "external"
                and isinstance(a, dict) and a.get("type") == "queued_command"
                and a.get("commandMode") == "prompt"
                and isinstance(a.get("prompt"), str)):
            return a["prompt"]
        return None
    if entry.get("type") != "user" or entry.get("userType") != "external":
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return None
    texts = []
    for block in content:
        if not isinstance(block, dict):
            return None
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "image":
            continue
        else:
            return None  # tool_result or anything unknown
    return "\n".join(texts) if texts else None


def is_injected(text):
    if any(m in text for m in RECAP_MARKERS):
        return True
    return text.lstrip().startswith(PREFIX_MARKERS)


def read_entries(path):
    """Every JSON object line of a transcript, in file order."""
    out = []
    with open(path, "r", encoding="utf-8", errors="strict") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
    return out


def owner_turns(path, entries=None):
    """Yield (entry, text) for every owner turn of a transcript file."""
    for entry in read_entries(path) if entries is None else entries:
        text = entry_text(entry)
        if text is None or is_injected(strip_outbound(text)):
            continue
        yield entry, text


def main_chain(entries):
    """uuids on the parentUuid walk back from the newest non-sidechain entry
    (check 6).  A compact boundary continues through logicalParentUuid."""
    by_uuid, leaf = {}, None
    for e in entries:
        u = e.get("uuid")
        if isinstance(u, str) and u and not e.get("isSidechain"):
            by_uuid[u] = e
            leaf = e
    chain, cur = set(), leaf
    while cur is not None and cur["uuid"] not in chain:
        chain.add(cur["uuid"])
        parent = cur.get("parentUuid") or cur.get("logicalParentUuid")
        cur = by_uuid.get(parent) if isinstance(parent, str) else None
    return chain


def bind_ledger(rows, turns):
    """Pair ledger rows with on-chain owner turns, in order per sha.
    Returns ({entry uuid: row}, [rows with no turn])."""
    pending = collections.defaultdict(list)
    for r in rows:
        pending[r.get("sha")].append(r)
    bound = {}
    for entry, text in turns:
        queue = pending.get(turn_sha(text))
        if queue:
            bound[entry.get("uuid")] = queue.pop(0)
    return bound, [r for q in pending.values() for r in q]


def chain_turns(path):
    """(on-chain owner turns, all owner turns, chain uuids, entries) of a
    transcript."""
    entries = read_entries(path)
    chain = main_chain(entries)
    every = list(owner_turns(path, entries))
    return [t for t in every if t[0].get("uuid") in chain], every, chain, entries


def assistant_texts(entry):
    """Text blocks of an assistant entry ([] for anything else)."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return []
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
                and isinstance(c.get("text"), str)]
    return []


def echoes_before(entries):
    """{owner entry uuid: NFC text of every assistant reply before it}.
    A quote found in there is the agent's line coming back (grill-2 fix 5:
    the owner pasted it to reject it), not the owner's own words."""
    out, said = {}, []
    for e in entries:
        said.extend(_nfc(t) for t in assistant_texts(e))
        u = e.get("uuid")
        if entry_text(e) is not None and isinstance(u, str):
            out[u] = "\n".join(said)
    return out


# Grill-3 fix 6: the agent's line can reach the owner outside this
# transcript -- a push.sh bubble (the outbound ledger) or a reply in an
# earlier session -- and come back pasted.  Both are searched for lines
# sent BEFORE the turn.
RECENT_SESSION_DAYS = 14
RECENT_SESSION_MAX = 50
OUTBOUND_LEDGER = "outbound-ledger.jsonl"
_TAG = re.compile(r"<[^>]+>")


def _parse_ts(s):
    return _store().parse_iso(s)


def _outbound_ledgers():
    """push.sh writes <root>/.telegram_bot/outbound-ledger.jsonl; the store's
    bot data dir may be another one (DOGANY_BOT_DATA_DIR).  Both."""
    dirs = [_store().bot_data_dir(), os.path.join(_ROOT, ".telegram_bot")]
    if OUTBOUND_CONTEXT is not None:
        return OUTBOUND_CONTEXT.ledger_paths(dirs[0], dirs[1:])
    out = []
    for d in dirs:
        p = os.path.realpath(os.path.join(d, OUTBOUND_LEDGER))
        if p not in out:
            out.append(p)
    return out


def _ledger_rows(paths):
    """DGN-1951: the bridge's ledger reader (bridge/outbound_context.py) --
    one reader for the Why net and the bridge's outbound-context block."""
    if OUTBOUND_CONTEXT is not None:
        for row in OUTBOUND_CONTEXT.iter_ledger_rows(paths):
            yield row
        return
    for path in paths:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("body"), str):
                    yield row


def _outbound_echo(q, before):
    for row in _ledger_rows(_outbound_ledgers()):
        ts = _parse_ts(row.get("ts"))
        if ts is not None and before is not None and ts >= before:
            continue
        import html  # lazy: the hook path never needs it (AMEND-2 G4)
        if q in quote_key(html.unescape(_TAG.sub("", row["body"]))):
            return True
    return False


def _session_echo(q, before, path):
    """q in an assistant reply (sent before `before`) of another recent
    session of the same project dir."""
    d = os.path.dirname(path)
    floor = (before if before is not None else 0) - RECENT_SESSION_DAYS * 86400
    try:
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        return False
    files = []
    for n in names:
        p = os.path.join(d, n)
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if os.path.realpath(p) != os.path.realpath(path) and m >= floor:
            files.append((m, p))
    for _, p in sorted(files, reverse=True)[:RECENT_SESSION_MAX]:
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ts = _parse_ts(e.get("timestamp")) if isinstance(e, dict) else None
                if ts is not None and before is not None and ts >= before:
                    continue
                if any(q in quote_key(t) for t in assistant_texts(e)):
                    return True
    return False


def outside_echo(q, entry, path):
    """Grill-3 fix 6: q was the agent's before this owner turn, outside
    this transcript (push bubble or an earlier session's reply)."""
    before = _parse_ts(entry.get("timestamp"))
    q = quote_key(q)
    return _outbound_echo(q, before) or _session_echo(q, before, path)


# Quote markup (grill-2 fix 5): a line led by ">" or a span wrapped in a
# quote pair carries someone else's words.
QUOTE_PAIRS = {'"': '"', "'": "'", "`": "`", "\u201c": "\u201d", "\u2018": "\u2019",
               "\u300c": "\u300d", "\u300e": "\u300f", "\u00ab": "\u00bb"}
QUOTE_LINE_HEADS = (">", "\uff1e")


# Grill-2 fix 8: a question is not a Why.  A quote that ends in a question
# mark, or whose clause in the turn does, or that ends on a Korean
# interrogative ending, refuses `quote-question`.
def question_verdict(text, quote):
    """None unless every occurrence of quote in text reads as a question
    (store.QUESTION_MARKS / QUESTION_ENDINGS)."""
    QUESTION_MARKS = _store().QUESTION_MARKS
    QUESTION_ENDINGS = _store().QUESTION_ENDINGS
    q = quote.rstrip(" \t.!~\u2026")
    if q.endswith(QUESTION_MARKS) or q.endswith(QUESTION_ENDINGS):
        return "quote-question"
    i, asked = text.find(quote), False
    while i >= 0:
        tail = text[i + len(quote):].lstrip(" \t")
        if not tail.startswith(QUESTION_MARKS):
            return None
        asked = True
        i = text.find(quote, i + 1)
    return "quote-question" if asked else None


def in_quote_markup(text, i, j):
    line = text[text.rfind("\n", 0, i) + 1:]
    if line.lstrip().startswith(QUOTE_LINE_HEADS):
        return True
    head = text[:i].rstrip(" \t")
    tail = text[j:].lstrip(" \t")
    return bool(head) and head[-1] in QUOTE_PAIRS and tail[:1] == QUOTE_PAIRS[head[-1]]


def _env_refusal(env):
    if env.get(DISPATCH_ENV_MARKER):
        return Refusal("dispatch-worker")
    if env.get("CLAUDECODE") != "1":
        return Refusal("resolver-unavailable")
    return None


def numeric_reply(reply):
    """A typed whole-message number, with an optional dot or beon suffix."""
    match = re.fullmatch(r"([1-9][0-9]*)(?:\ubc88|\.)?", reply.strip())
    return match.group(1) if match else None


def numeric_slot(reply, size):
    """Bind a numeric reply to a slot within this menu's range."""
    number = numeric_reply(reply)
    if number and len(number) <= len(str(size)):
        slot = int(number)
        if 1 <= slot <= size:
            return slot
    return None


def confirm_body(body, candidate):
    """The persona question line followed only by the verbatim candidate."""
    stem, sep, text = body.partition("\n")
    if not sep or not candidate or text not in (candidate, candidate + "\n") or body.count(candidate) != 1 \
            or not any(mark in stem for mark in _store().QUESTION_MARKS):
        return False
    try:
        from user_yes import vocab
        vetoes = vocab().get("hold")
        return bool(vetoes) and not any(p.search(stem) for p in vetoes)
    except (ImportError, OSError, ValueError, re.error):
        return False


def _confirmation_menu(entries, chain, entry, candidate, quote, row):
    """Bind a pick to the nearest authored menu on this turn's ancestry.

    A short label selects its numbered body item, never a candidate elsewhere
    in the reply. Legacy single-candidate menus use a standalone quotation
    and a first-slot confirmation question. No outbound context is authority.
    """
    by_uuid = {e.get("uuid"): e for e in entries if e.get("uuid") in chain}
    cur, seen = entry, set()
    said = None
    while cur and cur.get("uuid") not in seen:
        seen.add(cur.get("uuid"))
        parent = cur.get("parentUuid") or cur.get("logicalParentUuid")
        cur = by_uuid.get(parent)
        if cur is None:
            break
        texts = assistant_texts(cur)
        if any("[[OPTIONS" in text for text in texts):
            said = "\n".join(texts).strip()
            break
    if not said or "```" in said:
        return False
    # Accept only authored labeled markers or a bare marker followed by a
    # contiguous, position-numbered menu. Unknown menu shapes fail closed.
    match = re.fullmatch(r"(.*?)\[\[OPTIONS:\s*([^\n]+)\]\]", said, re.S)
    if match:
        body, raw = match.groups()
        labels = [s.strip() for s in raw.split("|")]
    else:
        parts = said.split("[[OPTIONS]]")
        if len(parts) != 2:
            return False
        body, raw = parts
        labels = []
        for i, line in enumerate(raw.strip().splitlines(), 1):
            m = re.fullmatch(r"%d\. (.+)" % i, line.strip())
            if not m:
                return False
            labels.append(m.group(1))
    if ("[[OPTIONS" in body or not all(labels) or not labels
            or len(set(map(quote_key, labels))) != len(labels)):
        return False
    numeric = numeric_slot(quote, len(labels))
    picked = [i for i, label in enumerate(labels, 1)
              if quote_key(quote) in (quote_key(label), quote_key("%d. %s" % (i, label)))]
    if numeric and row.get("source") == "message":
        picked = [numeric]
    if len(picked) != 1:
        return False
    if "keyboard" in row or "option_index" in row:
        from why_net.ups import tap_slot
        shown = [{"slot": i, "label": label} for i, label in enumerate(labels, 1)]
        if tap_slot(quote, row, shown, False) != ("pick", picked):
            return False
    # Candidate matching is verbatim, unlike the label's whitespace folding.
    if not candidate:
        return False
    items = list(re.finditer(r"^(\d+)\. [^\n]+", body, re.M))
    if items:
        if [int(m.group(1)) for m in items] != list(range(1, len(labels) + 1)):
            return False
        i = picked[0] - 1
        end = items[i + 1].start() if i + 1 < len(items) else len(body)
        selected = body[items[i].end(1) + 2:end]
        # Unindented prose after an item belongs to the reply, not the pick.
        # Indented continuations and displayed blockquotes stay with the item.
        selected = re.split(r"\n(?=\S)(?!> )", selected, maxsplit=1)[0]
        return selected.count(candidate) == 1

    # The persona confirm shape puts its question before the verbatim sentence.
    # Its two choices bind by index, independently of their restyled words.
    if confirm_body(body, candidate) and len(labels) == 2:
        return picked == [1]

    # The legacy yes/no shape has one quoted candidate and two choices. Only
    # the first choice affirms it; the final question must ask that action.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    if len(labels) != 2 or picked != [1] or len(paragraphs) < 2:
        return False
    question = quote_key(paragraphs[-1])
    if (not question.casefold().startswith(quote_key(labels[0]).casefold())
            or not any(mark in question for mark in _store().QUESTION_MARKS)):
        return False
    # Reuse the affirmative reader's veto vocabulary; contradictory riders
    # cannot turn a first-slot label into confirmation of the quotation.
    try:
        from user_yes import vocab
        vetoes = vocab().get("hold")
        if not vetoes or any(p.search(question) for p in vetoes):
            return False
    except (ImportError, OSError, ValueError, re.error):
        return False
    quoted = {candidate, "> " + candidate}
    quoted.update(left + candidate + right for left, right in QUOTE_PAIRS.items())
    for p in paragraphs[:-1]:
        is_quoted = p.startswith("> ") or any(
            p.startswith(left) and p.endswith(right) for left, right in QUOTE_PAIRS.items())
        if is_quoted and p not in quoted:
            return False
    return sum(p in quoted for p in paragraphs[:-1]) == 1


def resolve(quote, session_id, message_uuid=None, cwd=None, env=None,
            audit=False, allow_past=False, candidate=None):
    """Agent-quote resolver.  audit=True (nightly `derive --audit`) re-runs
    the transcript + ledger + clause checks on a recorded confirm and skips
    the live-call checks (worker env, runtime, current session).
    allow_past is used only by confirm: all transcript/ledger checks remain.
    candidate verifies a menu selection, never attributes its label to owner wording.
    """
    env = os.environ if env is None else env
    if not audit:
        bad = _env_refusal(env)
        if bad:
            return bad
    if not isinstance(quote, str) or not quote.strip():
        return Refusal("empty-quote")
    if not audit and not allow_past and (not session_id or session_id != current_session(env)):
        return Refusal("not-current-session")
    path = session_transcript(session_id, cwd, env)
    if not path:
        return Refusal("resolver-unavailable")
    q = _nfc(quote)
    pattern = _quote_pattern(q)
    try:
        on_chain, every, chain, entries = chain_turns(path)
    except (OSError, UnicodeDecodeError):
        return Refusal("resolver-unavailable")
    bound, _ = bind_ledger(ledger_rows(session_id), on_chain)
    echoes = echoes_before(entries)
    found, why = None, set()
    for entry, text in every:
        if message_uuid and entry.get("uuid") != message_uuid:
            continue
        t = _nfc(text)
        if not pattern.search(t):
            continue
        lo, hi = owner_region(t)
        spans = [m.group() for m in pattern.finditer(t[lo:hi])]
        if not spans:
            # grill-3 fix 7: bridge-written wrapper text (photo / document)
            why.add("quote-wrapper")
            continue
        t = t[lo:hi]
        if entry.get("uuid") not in chain:
            why.add("turn-off-chain")
            continue
        row = bound.get(entry.get("uuid"))
        if row is None:
            why.add("turn-not-in-ledger")
            continue
        if row.get("source") == "callback" and candidate is None:
            # grill-3 fix 4: a tap's text is a button label the agent wrote
            # (an inline ask or a push.sh keyboard); it confirms only through
            # the probe path (confirm_tap, worded_by=agent), never as a quote
            why.add("quote-tap")
            continue
        if row.get("source") == "voice":
            # grill-2 fix 4: a Whisper transcript (hallucinations included)
            # seeds a hypothesis at most; a confirm needs typed words or a tap
            why.add("quote-voice")
            continue
        tp = row.get("transcript_path")
        if isinstance(tp, str) and os.path.realpath(tp) != os.path.realpath(path):
            # the hook logged this turn in another file (a CLAUDE_CONFIG_DIR
            # redirect onto a forged copy, grill-2 N3)
            why.add("transcript-mismatch")
            continue
        bad = [span_verdict(t, s) or question_verdict(t, s) for s in spans]
        if all(bad):
            why.update(bad)
            continue
        if candidate is not None:
            if quote_key(t) != quote_key(q) or not _confirmation_menu(
                    entries, chain, entry, candidate, q, row):
                why.add("quote-tap")
                continue
            found = (entry, text)
            continue
        if quote_key(q) in quote_key(echoes.get(entry.get("uuid"), "")) \
                or outside_echo(q, entry, path):
            why.add("quote-echo")
            continue
        found = (entry, text)  # newest match wins
    if not found:
        for reason in REFUSAL_ORDER:
            if reason in why:
                return Refusal(reason)
        return Refusal("quote-not-owner-turn")
    entry, text = found
    return TurnRef(RUNTIME, session_id, entry.get("uuid"),
                   entry.get("timestamp"), text)


def owner_span(text, quote):
    """The owner's original bytes, including spacing and NFD, for a quote.
    Falls back to the quote when no normalized span maps back."""
    q = _nfc(quote)
    idx, flat = [], []
    for k, ch in enumerate(text):
        for d in unicodedata.normalize("NFD", ch):
            flat.append(d)
            idx.append(k)
    flat = "".join(flat)
    needle = unicodedata.normalize("NFD", q)
    for match in _quote_pattern(needle).finditer(flat):
        if not match.group():
            continue
        lo, hi = idx[match.start()], idx[match.end() - 1] + 1
        if quote_key(text[lo:hi]) == quote_key(q):
            return text[lo:hi]
    return quote


def resolve_prompt(verbatim, session_id, env=None, audit=False):
    """Tap/pick resolver for `why-net ups` (R1g): the probe verbatim must BE
    a ledgered owner prompt of that session (byte-exact after NFC).  The
    ledger row was written from the hook stdin of that very turn, so this
    does not wait for the transcript (V-1); the transcript only lends the
    message uuid when the entry is already there."""
    env = os.environ if env is None else env
    if not audit:
        bad = _env_refusal(env)
        if bad:
            return bad
    if not isinstance(verbatim, str) or not verbatim.strip() or not session_id:
        return Refusal("empty-quote")
    if is_injected(verbatim):
        return Refusal("quote-not-owner-turn")
    sha = turn_sha(verbatim)
    rows = _rows_of_sha(session_id, sha)
    if not rows:
        return Refusal("turn-not-in-ledger")
    uuid = None
    path = session_transcript(session_id, None, env)
    on_chain = []
    if path:
        try:
            on_chain = chain_turns(path)[0]
        except (OSError, UnicodeDecodeError):
            on_chain = []
    bound, _ = bind_ledger(ledger_rows(session_id), on_chain)
    for entry, text in on_chain:
        if sha in (turn_sha(text), turn_sha(strip_outbound(text))) \
                and entry.get("uuid") in bound:
            uuid = entry.get("uuid")
    if audit and uuid is None:
        # Live, the tap's own entry may not be logged yet (V-1); the audit
        # runs later and needs the real on-chain turn (grill-2 fix 2).
        return Refusal("turn-off-chain" if path else "resolver-unavailable")
    return TurnRef(RUNTIME, session_id, uuid, rows[-1].get("at"),
                   _nfc(verbatim))


def prompt_otl(verbatim, session_id):
    """The otl of the newest ledgered owner prompt of a session that IS
    `verbatim` (the row resolve_prompt binds a tap to), or None."""
    if not isinstance(verbatim, str) or not session_id:
        return None
    rows = _rows_of_sha(session_id, turn_sha(verbatim))
    return rows[-1].get("id") if rows else None


def _rows_of_sha(session_id, sha):
    """Ledgered owner prompts of a session whose full text or owner region
    (owner_sha: the turn carried an outbound-context block) is `sha`."""
    return [r for r in ledger_rows(session_id)
            if sha in (r.get("sha"), r.get("owner_sha"))]


def turn_otl(session_id, message_uuid, env=None):
    """AMEND-1 1.3: the turn-ledger id (otl-N) of the on-chain owner turn
    `message_uuid` of a session, or None when that turn is not ledgered."""
    if not session_id or not message_uuid:
        return None
    path = session_transcript(session_id, None, env)
    if not path:
        return None
    try:
        on_chain = chain_turns(path)[0]
    except (OSError, UnicodeDecodeError):
        return None
    bound, _ = bind_ledger(ledger_rows(session_id), on_chain)
    row = bound.get(message_uuid)
    return row.get("id") if row else None


def audit_ledger(env=None):
    """Grill-2 fixes 1-3: every ledger row must be a real turn -- an owner
    entry on its session's main chain, paired 1:1 in order per sha.  A row
    an agent wrote (Bash `>>`, or `why-net ups` run from its own tool call)
    has no such entry.  Returns [{"row", "session_id", "reason"}]."""
    env = os.environ if env is None else env
    by_session = collections.OrderedDict()
    for r in ledger_rows():
        by_session.setdefault(r.get("session_id"), []).append(r)
    out = []
    for sid, rows in by_session.items():
        path = session_transcript(sid, None, env) if isinstance(sid, str) else None
        if not path:
            out.extend({"row": r.get("id"), "session_id": sid,
                        "reason": "no-transcript"} for r in rows)
            continue
        try:
            on_chain = chain_turns(path)[0]
        except (OSError, UnicodeDecodeError):
            on_chain = []
        _, unbound = bind_ledger(rows, on_chain)
        out.extend({"row": r.get("id"), "session_id": sid,
                    "reason": "no-real-turn"} for r in unbound)
    return out
