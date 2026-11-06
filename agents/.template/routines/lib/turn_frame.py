"""turn_frame -- per-turn problem frame (DGN-2028 v2.7 merged design s4,
build step 2).

Three consumers, one module:
  - memory.py hook (UserPromptSubmit): build_frame() renders the [turn frame]
    block that follows the time line, and records the frame as one JSONL
    state-transition row in a private per-session file. Loaded only when
    TURN_FRAME is on, so with the flag off nothing here runs.
  - memory.py recall render: explanation_label() marks a recalled line that
    carries the explanation slot token or an uncertain causal cue.
  - routines/turn-frame.py (Stop): stop_findings() runs S1-S4.

The hook emits an instruction and the facts it has; it never states the
problem or the owner's Why itself (a cue match is not a verified problem).

Latency: the frame lane reads bounded local files only -- no model call, no
kit subprocess, no network, no event-history fold. The caller passes a
monotonic deadline; every step checks it and raises FrameTimeout past it.

Turn identity: instance root + session id + the bridge's request id for
THIS prompt (inbound-context text_sha must match). Without a request id the
frame gets a fresh nonce: a repeated identical prompt is a new turn, never a
match on prompt hash or on "the last frame". Same-session carry is not
implemented in this step (no work item to check continuity against), so
every owner turn opens its own frame.

Store: <bot data dir>/state/turn-frame/<session key>.jsonl, directory 0700,
file 0600. Rows hold references and hashes, never prompt or reply text.
Rows are appended, never edited. A failed record emits no frame id, and
Stop never infers a violation from a missing frame.
"""
import hashlib
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LEXICON_PATH = os.path.join(HERE, "turn_frame_lexicon.json")
FRAME_MARKER = "[turn frame]"
FRAME_VERSION = 1
STORE_SUB = os.path.join("state", "turn-frame")
MAX_STORE_READ = 512 * 1024
EXPLANATION_LABEL = "[explanation, uncertain]"
# One RECALL_FRAME_MARKER-marked frame line, appended to the recall frame only when a
# rendered line carries the explanation label (the marker itself is defined
# in memory.py; the sentence is agent-facing English).
EXPLANATION_RULE = ("lines marked %s are a past interpretation, not today's cause: "
                    "treat one as a candidate cause and verify it against current, "
                    "relevant evidence before stating it as the cause."
                    % EXPLANATION_LABEL)
# 18:51 addendum to the merged design (owner: analysis before measurement): a problem-report
# turn opens with analysis -- Why, observed now, candidate causes -- and a
# measurement verifies a named cause; it is never the first move.
PROBLEM_REPORT = "problem-report"
PROBLEM_REPORT_RULE = (
    "analysis first: state the owner's Why (or that it is unknown), what is "
    "observed now, then break the gap into candidate causes against the kit's "
    "goals, resources and constraints; verify the named causes cheapest check "
    "first -- a measurement is evidence for a named cause, never the first move")

_LEX = []
_SOURCE_LOADER = []


def source_loader():
    """The shared install/runtime contract implementation, loaded once."""
    if not _SOURCE_LOADER:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_tf_base_contract", os.path.join(HERE, "base_contract.py"))
        bc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bc)
        _SOURCE_LOADER.append(bc)
    return _SOURCE_LOADER[0]


class FrameTimeout(Exception):
    pass


def lexicon(path=None):
    """The data-file lexicon (cached per process). Missing/broken -> {}."""
    if path:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    if not _LEX:
        try:
            with open(LEXICON_PATH, encoding="utf-8") as fh:
                _LEX.append(json.load(fh))
        except (OSError, ValueError):
            _LEX.append({})
    return _LEX[0]


def _cue_hit(text, cues):
    """True when any cue occurs: ASCII cues as whole words (case-folded),
    other cues as substrings."""
    low = text.lower()
    for lst in (cues or {}).values():
        for c in lst:
            cl = c.lower()
            if cl.isascii():
                if re.search(r"(?<![a-z0-9])" + re.escape(cl) + r"(?![a-z0-9])", low):
                    return True
            elif cl in low:
                return True
    return False


# ---- explanation label (read time only) -------------------------------

def is_explanation(text, lex=None):
    lex = lexicon() if lex is None else lex
    tok = lex.get("slot_token_re")
    if tok and re.search(tok, text or ""):
        return True
    return _cue_hit(text or "", lex.get("explanation_cues"))


def label_lines(lines, bodies, lex=None):
    """Append EXPLANATION_LABEL to each rendered line whose body reads as an
    explanation and move those lines last (stable). Returns (lines, n)."""
    lex = lexicon() if lex is None else lex
    plain, marked = [], []
    for ln, body in zip(lines, bodies):
        if is_explanation(body, lex):
            marked.append(ln + " " + EXPLANATION_LABEL)
        else:
            plain.append(ln)
    return plain + marked, len(marked)


# ---- flags -------------------------------------------------------------

def conf_get(key, conf_path):
    """Last `KEY=value` of a shell-style conf file, or None."""
    val = None
    try:
        with open(conf_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        return None
    return val


def flag(key, conf_path, default=""):
    raw = os.environ.get("DOGANY_" + key)
    if raw is None or raw == "":
        raw = conf_get(key, conf_path) if conf_path else None
    return (raw if raw not in (None, "") else default).strip().lower()


def frame_enabled(conf_path):
    return flag("TURN_FRAME", conf_path) in ("1", "on", "true", "yes")


# ---- store -------------------------------------------------------------

def bot_data_dir(root):
    return os.environ.get("DOGANY_BOT_DATA_DIR") or os.path.join(root, ".telegram_bot")


def session_key(session_id):
    return hashlib.sha256((session_id or "-").encode("utf-8")).hexdigest()[:24]


def store_path(root, session_id):
    return os.path.join(bot_data_dir(root), STORE_SUB, session_key(session_id) + ".jsonl")


def _sha(text):
    import unicodedata
    return hashlib.sha256(unicodedata.normalize("NFC", text or "").encode("utf-8")).hexdigest()


def append_row(path, row, deadline=None):
    """Append one JSON row under an exclusive lock; private permissions.
    Raises on failure (the caller decides what a failure means)."""
    import fcntl
    d = os.path.dirname(path)
    old = os.umask(0o077)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    finally:
        os.umask(old)
    try:
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise FrameTimeout("lock")
                time.sleep(0.005)
        os.write(fd, (json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii"))
    finally:
        os.close(fd)


def read_rows(path):
    """Rows of a session frame file (tail-bounded); [] when absent."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - MAX_STORE_READ))
            raw = fh.read()
    except OSError:
        return []
    out = []
    for ln in raw.split(b"\n"):
        try:
            r = json.loads(ln.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


# ---- frame build (hook lane) -----------------------------------------

def _check(deadline):
    if time.monotonic() >= deadline:
        raise FrameTimeout("budget")


def _request_id(root, prompt):
    """The bridge request id naming THIS prompt, else None."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_tf_decision_ledger_fmt", os.path.join(HERE, "decision_ledger_fmt.py"))
        fmt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fmt)
        return fmt.request_id_for(fmt.read_inbound(bot_data_dir(root), time.time()), prompt)
    except Exception:
        return None


def match_workflow(prompt, contracts):
    """First declared workflow whose cue occurs in the prompt, scanning kits
    in pack-id order: (pack_id, workflow) or None. A cue match proposes a
    candidate workflow only."""
    low = (prompt or "").lower()
    hits = []
    for pid, c in contracts:
        for wf in c.get("workflows") or []:
            for lst in (wf.get("cues") or {}).values():
                if any(x.lower() in low for x in lst):
                    hits.append((pid, wf))
                    break
    return hits


def frame_class(prompt, lex=None):
    """PROBLEM_REPORT when the prompt carries a problem-report cue (data in
    the lexicon's frame_classes; `cue_res` holds boundary-anchored regexes
    for short cues such as the Korean word for "again", which as a substring would
    also match the Korean word for "or"),
    else None. A cue match selects the frame's opening instruction only; it
    never states the problem."""
    lex = lexicon() if lex is None else lex
    c = (lex.get("frame_classes") or {}).get(PROBLEM_REPORT) or {}
    text = prompt or ""
    if _cue_hit(text, c.get("cues")):
        return PROBLEM_REPORT
    for pat in c.get("cue_res") or []:
        if re.search(pat, text, re.IGNORECASE):
            return PROBLEM_REPORT
    return None


# DGN-2028 AMEND-2 2.8 (F19, F27): with WHY_NET on the hook still reads no
# Why store; it points the agent at the verb that frames the Why in-turn.
WHY_LINE_OFF = "why: unknown to this hook -- do not present a Why as confirmed"
WHY_LINE_ON = "why: unknown to this hook; use four-slot work intake before consequential work"


def why_net_on(conf_path):
    """WHY_NET from the conf the lane already reads (no store read)."""
    return flag("WHY_NET", conf_path) == "on"


def render(frame, why_on=False):
    """The [turn frame] block (agent-facing English; every line carries the
    noise sentinel so the frame never consolidates itself).  `why_on`:
    WHY_NET is on (the static frame-verb line, AMEND-2 2.8)."""
    m = FRAME_MARKER
    lines = []
    if frame.get("frame_id"):
        lines.append("%s id %s" % (m, frame["frame_id"]))
    wfs = frame.get("workflows") or []
    if wfs:
        lines.append("%s candidate workflow (cue match, unverified): %s" % (m, "; ".join(
            "%s/%s target=%s stakes=%s" % (w["pack"], w["id"], w["target"], w["stakes"])
            for w in wfs)))
    else:
        lines.append("%s candidate workflow: none matched" % m)
    lines.append("%s %s" % (m, WHY_LINE_ON if why_on else WHY_LINE_OFF))
    if frame.get("class") == PROBLEM_REPORT:
        lines.append("%s class: problem-report (cue match, unverified) -- %s"
                     % (m, PROBLEM_REPORT_RULE))
    else:
        lines.append("%s before acting, state in one line: the request, the observed gap "
                     "(or the observation still missing), and its target" % m)
    lines.append("%s order below: workflow evidence, then memory as dated evidence, "
                 "then conversation" % m)
    return "\n".join(lines)


def build_frame(prompt, session_id, root, deadline, log=None, raw_prompt=None,
                resolved_sources=None):
    """Compute, record and render the frame. Returns the block or raises
    FrameTimeout. A record failure renders the frame without an id.
    `prompt` (stripped) drives cue matching; `raw_prompt` (as the transcript
    holds it) drives the request-id binding and the Stop match."""
    raw = raw_prompt if isinstance(raw_prompt, str) else prompt
    _check(deadline)
    bc = source_loader()
    contracts = (resolved_sources["contracts"] if resolved_sources is not None
                 else bc.load_installed(root, deadline=deadline))
    _check(deadline)
    wfs = [{"pack": pid, "id": wf["id"], "target": wf["target"],
            "stakes": wf.get("stakes_hint", "normal")}
           for pid, wf in match_workflow(prompt, contracts)][:3]
    fclass = frame_class(prompt)
    rid = _request_id(root, raw)
    _check(deadline)
    import uuid  # lazy: only an unbound turn needs a nonce
    nonce = None if rid else uuid.uuid4().hex
    ident = "%s|%s|%s" % (os.path.realpath(root), session_id or "-", rid or nonce)
    frame = {
        "v": FRAME_VERSION, "ev": "issued",
        "frame_id": "tf-" + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16],
        "session": session_key(session_id), "request_id": rid, "nonce": nonce,
        "prompt_sha": _sha(raw), "ts": time.time(),
        "workflows": wfs, "class": fclass,
        "stakes": "high" if any(w["stakes"] == "high" for w in wfs) else (
            "normal" if wfs else "none"),
        "asks": [],
    }
    if resolved_sources is not None:
        frame["sources"] = bc.source_refs(resolved_sources)
        frame["source_discovery"] = resolved_sources["discovery"]
        frame["source_overlaps"] = resolved_sources.get("overlaps", [])
    try:
        append_row(store_path(root, session_id), frame, deadline)
    except FrameTimeout:
        raise
    except Exception as e:
        if log:
            log("turn-frame record failed: %s" % type(e).__name__)
        frame = dict(frame, frame_id=None)
    _check(deadline)
    conf = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(root, "config", "agent.conf")
    return render(frame, why_net_on(conf))


# ---- Stop checks -----------------------------------------------------

_SENT_SPLIT = re.compile(r"(?<=[.!?\u3002])\s+|\n+")
_QUOTE_SPANS = re.compile(r"\"[^\"]*\"|\u201c[^\u201d]*\u201d|\u300c[^\u300d]*\u300d|`[^`]*`")


def current_frame(rows, prompt, after_ts):
    """The issued frame of this turn: matching prompt hash, recorded after
    the previous turn ended. None when absent (never 'the last frame').

    DGN-2028 AMEND-2 2.5 / 2.10: the frame's transition rows are merged in
    -- `why` rows (written by `why-net frame` after its own lock) record the
    disposition and the turn ask (S2 checks it is carried); `disposition`
    rows (`turn-frame.py disposition`, the WHY_NET-off path) record the
    disposition only."""
    want = _sha(prompt)
    best = None
    for r in rows:
        if (r.get("ev") == "issued" and r.get("frame_id") and r.get("prompt_sha") == want
                and (after_ts is None or (r.get("ts") or 0) > after_ts)):
            best = r
    if best is None:
        return None
    frame = dict(best, asks=list(best.get("asks") or []))
    for r in rows:
        if r.get("frame_id") != best["frame_id"]:
            continue
        if r.get("ev") == "why":
            frame["disposition"] = r.get("disposition")
            if r.get("ask") and r.get("disposition") == "asked" \
                    and all(a.get("ask") != r["ask"] for a in frame["asks"]):
                frame["asks"].append({"ask": r["ask"], "otl": r.get("otl")})
        elif r.get("ev") == "disposition":
            frame["disposition"] = r.get("value")
    return frame


def causal_assertions(text, lex=None):
    """Sentences that assert a present-case cause: a causal cue, outside
    quotes/blockquotes, not negated, not hedged, not historical, not a
    question. Returns [(sentence, topic or None)]."""
    lex = lexicon() if lex is None else lex
    out = []
    for raw in (text or "").splitlines():
        if raw.lstrip().startswith(">"):
            continue
        for s in _SENT_SPLIT.split(raw):
            s = s.strip()
            if not s or s.endswith(("?", "\uff1f")):
                continue
            bare = _QUOTE_SPANS.sub(" ", s)
            if not _cue_hit(bare, lex.get("causal_cues")):
                continue
            if any(_cue_hit(bare, lex.get(k)) for k in
                   ("negation_cues", "hedge_cues", "historical_cues")):
                continue
            topic = None
            for name, t in (lex.get("topics") or {}).items():
                if _cue_hit(bare, t.get("cues")):
                    topic = name
                    break
            out.append((s, topic))
    return out


def has_evidence(topic, tools, prompt, lex=None):
    """True when THIS turn holds relevant successful evidence for `topic`:
    a tool call matching the topic's evidence commands whose result is not
    an error, or a measurement already cited in the turn's prompt."""
    lex = lexicon() if lex is None else lex
    t = (lex.get("topics") or {}).get(topic) or {}
    pats = [re.compile(p) for p in t.get("evidence_commands") or []]
    for tool in tools:
        cmd = tool.get("command") or ""
        if tool.get("ok") and cmd and any(p.search(cmd) for p in pats):
            return True
    return any(m in (prompt or "") for m in t.get("prompt_measurement") or [])


def stop_findings(frame, reply, tools, prompt, display_on, lex=None, carried=None):
    """S1-S4 over one turn. Returns [{"check", "detail"}] (references and
    counts only, no reply text).

    S1  high-stakes frame without a recorded disposition
    S2  an ask recorded on the frame not carried by the reply (carried(ask))
    S3  problem-line display on, problem-solving reply, approved prefix absent
    S4  present-case causal assertion without relevant successful evidence
        (detect-only in 2.7, whatever the mode)"""
    lex = lexicon() if lex is None else lex
    out = []
    if frame and frame.get("stakes") == "high" and not frame.get("disposition"):
        out.append({"check": "S1", "detail": "high-stakes frame has no disposition"})
    for ask in (frame or {}).get("asks") or []:
        if carried is None or not carried(reply, ask):
            out.append({"check": "S2", "detail": "ask %s not carried" % ask.get("ask")})
    if display_on and frame and frame.get("workflows"):
        prefixes = [p for lst in (lex.get("problem_line_prefixes") or {}).values() for p in lst]
        if not prefixes:
            out.append({"check": "S3", "detail": "skipped: no approved prefix"})
        elif len((reply or "").strip()) > 80 and not (reply or "").lstrip().startswith(tuple(prefixes)):
            out.append({"check": "S3", "detail": "problem line absent"})
    for _s, topic in causal_assertions(reply, lex):
        if topic is None:
            continue  # no declared topic: relevance cannot be judged, not flagged
        if not has_evidence(topic, tools, prompt, lex):
            out.append({"check": "S4", "detail": "topic=%s no relevant evidence" % topic})
    return out
