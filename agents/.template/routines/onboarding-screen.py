#!/usr/bin/env python3
"""DGN-1849 (dec-243): the onboarding verb -- fixed onboarding moments are
sent by the machine, not composed by the model.

Same contract as the staff-agent mint verb (scripts/pack/lib/mint_flow.py +
mint_screens.py): the verb renders the screen from the single-source copy
plus machine data, sends it itself through routines/push.sh --strict with
inline option buttons, and returns one JSON line telling the model to end
the turn (note_for_agent SCREEN_SENT_END_TURN, agent_utterance none).  The
bridge mutes the model's own text of such a turn (bridge/mint_gate.py reads
this result).  Knowledge that needs determinism goes to code, never prose
(DGN-1224): rehearsal 2026-10-04 measured model-composed questions that
dropped Q1 and invented possessives.

Steps (the order is fixed; a step is sent only when it is due):
  q1   name ask                 onboarding.q1, no buttons
  q2   emoji                    onboarding.q2 + the 3-4 emoji the model
                                passes (--emoji, validated: distinct, each
                                one single emoji) as buttons
  q3   address term             q3_candidates.py: zero ->
                                onboarding.q3 alone; one or more ->
                                onboarding.q3_candidates + candidate buttons
  q4   tone                     onboarding.q4 + the installed pack's tone
                                candidates (onboarding-check.py
                                resolve_tone_candidates) or the generic four
                                (onboarding.q4.toneN.*); rows in the body,
                                labels as buttons
  q5   humor                    onboarding.q5 + 10/30/50/70/90% buttons
  kit  kit offer (KIT=pending)  kit.offer + onboarding.kit.option_* buttons
Due step: the first identity field (Name / Emoji / Call the user / Tone /
Humor) still holding the "(set at onboarding" placeholder; all five filled
on a main whose kit choice is open (KIT=pending, life kit reachable) -> kit.
"next" sends whatever is due.  A step that is not due is refused (nothing
sent): the model records the owner's answer into identity first.

--ack TEXT: the model's short free reply to the owner's answer (acknowledge
/ confirm the saved value), followed by a blank line and the screen in one
message, with the screen's buttons attached (DGN-1866).
Copy is never in this file: it lives in config/i18n/<lang>.json (kit layers
first, routines/lib/i18n_lookup.py).

Result (one JSON line on stdout, rc 0; rc 2 = usage error):
  delivered   screen_delivered true,  note SCREEN_SENT_END_TURN
  uncertain   screen_delivered false, note SCREEN_UNCERTAIN_END_TURN
              (Telegram may have it -- end the turn, never re-send)
  failed      screen_delivered false, note SCREEN_FAILED_SEND_TEXT,
              agent_utterance fallback, owner_text = the screen as a reply
              the model sends verbatim ([[OPTIONS]] form)
  refused     screen_delivered false, note NOT_SENT_FIX_AND_RETRY, error
--dry-run renders without sending (note DRY_RUN).

Usage: onboarding-screen.py <q1|q2|q3|q4|q5|kit|next> [--emoji E]...
       [--ack TEXT] [--root DIR] [--dry-run]
English/ASCII only.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

try:
    from conf_reader import conf_get
except Exception:
    def conf_get(key, conf_path=None):
        return ""
try:
    import i18n_lookup
except Exception:
    i18n_lookup = None
try:
    from q3_candidates import candidates as _q3_candidates
except Exception:
    _q3_candidates = None
try:
    import first_contact_state as _fc_state
except Exception:
    _fc_state = None
try:
    import first_contact_terms as _fc_terms
except Exception:
    _fc_terms = None

STEPS = ("q1", "q2", "q3", "q4", "q5", "kit")
FIELD_STEPS = (
    ("q1", "- Name:"),
    ("q2", "- Emoji:"),
    ("q3", "- Call the user"),
    ("q4", "- Tone:"),
    ("q5", "- Humor:"),
)
PLACEHOLDER = "(set at onboarding"
HUMOR_OPTIONS = ("10%", "30%", "50%", "70%", "90%")
GENERIC_TONES = 4
EMOJI_MIN, EMOJI_MAX = 3, 4
ACK_MAX = 400
CALLBACK_MAX = 64             # Telegram callback_data limit, bytes
PUSH_TIMEOUT_S = 60
_CALL_RE = re.compile(r"^\s*-\s*Call the user\s*\*\*(.+?)\*\*")

NOTE_SENT = "SCREEN_SENT_END_TURN"
NOTE_UNCERTAIN = "SCREEN_UNCERTAIN_END_TURN"
NOTE_FAILED = "SCREEN_FAILED_SEND_TEXT"
NOTE_REFUSED = "NOT_SENT_FIX_AND_RETRY"
NOTE_DRY = "DRY_RUN"


class Refused(Exception):
    """Nothing was sent: bad arguments, step not due, or copy missing."""


# ------------------------------------------------------------------ loaders
def load_check(root):
    """onboarding-check.py (identity resolver, tone candidates, kit gate) of
    this routines dir; None when it cannot be loaded."""
    try:
        spec = importlib.util.spec_from_file_location(
            "onboarding_check_for_screen", os.path.join(HERE, "onboarding-check.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def instance_lang(root):
    lang = conf_get("AGENT_LANG", os.path.join(root, "config", "agent.conf")) or "en"
    return lang.lower().replace("_", "-").split("-")[0]


def copy(root, lang, key):
    """One i18n value (kit layers, then framework; lang, then en)."""
    val = i18n_lookup.lookup(root, lang, key) if i18n_lookup else ""
    if not val:
        raise Refused("copy key %s missing" % key)
    return val


def identity_path(root, check=None):
    check = check if check is not None else load_check(root)
    if check is None:
        return None
    try:
        path, _collision = check.resolve_persona_md(root)
        return path
    except Exception:
        return None


def _identity_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except Exception:
        return []


def field_unset(lines, prefix):
    for line in lines:
        if line.lstrip().startswith(prefix):
            return PLACEHOLDER in line
    return False


def address_term(lines):
    """The address term the owner chose at Q3, or ''."""
    for line in lines:
        m = _CALL_RE.match(line)
        if m:
            value = m.group(1).strip()
            return "" if PLACEHOLDER in value else value
    return ""


def kit_due(root, check):
    """All identity fields filled -> the kit offer is due on a main whose
    kit choice is open and whose life kit is reachable (the selector's own
    gate, DGN-1817)."""
    if check is None:
        return False
    data = {"cwd": root}
    try:
        if check.instance_class(check.resolve_instance_conf(data)) == "domain":
            return False
        if not check.kit_select_pending(check.resolve_kit(check.resolve_agent_conf(data))):
            return False
        return bool(check.life_kit_installable(data))
    except Exception:
        return False


def due_step(root, check=None, path=None):
    """The step whose screen is due now, or None (onboarding has nothing
    fixed left to send).  path: the identity file when the caller already
    resolved it (the gate honors ONBOARDING_FILE)."""
    check = check if check is not None else load_check(root)
    path = path or identity_path(root, check)
    lines = _identity_lines(path) if path else []
    if not lines:
        return None
    for step, prefix in FIELD_STEPS:
        if field_unset(lines, prefix):
            return step
    return "kit" if kit_due(root, check) else None


# ------------------------------------------------------------------ emoji
def _emoji_base(cp):
    if 0x1F1E6 <= cp <= 0x1F1FF or 0x1F3FB <= cp <= 0x1F3FF:
        return False                       # regional indicator / skin tone alone
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
            or 0x2300 <= cp <= 0x23FF or 0x2B00 <= cp <= 0x2BFF
            or 0x2190 <= cp <= 0x21FF or 0x2934 <= cp <= 0x2935
            or 0x25A0 <= cp <= 0x25FF
            or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139,
                      0x24C2, 0x3030, 0x303D, 0x3297, 0x3299))


def single_emoji(value):
    """True iff value is exactly one emoji (ZWJ sequence, flag, keycap,
    tag flag, with optional VS16 / skin tone)."""
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    cps = [ord(c) for c in value]
    if len(cps) == 2 and all(0x1F1E6 <= c <= 0x1F1FF for c in cps):
        return True
    if re.fullmatch("[0-9#*]\ufe0f?\u20e3", value):
        return True
    if cps[0] == 0x1F3F4 and len(cps) > 2 and cps[-1] == 0xE007F \
            and all(0xE0020 <= c <= 0xE007F for c in cps[1:]):
        return True
    for unit in value.split("\u200d"):
        ucp = [ord(c) for c in unit]
        if not ucp or not _emoji_base(ucp[0]) or len(ucp) > 3:
            return False
        for c in ucp[1:]:
            if c != 0xFE0F and not 0x1F3FB <= c <= 0x1F3FF:
                return False
    return True


def validate_emoji(items):
    items = [unicodedata.normalize("NFC", e) for e in (items or [])]
    if not EMOJI_MIN <= len(items) <= EMOJI_MAX:
        raise Refused("q2 needs %d-%d --emoji arguments, got %d"
                      % (EMOJI_MIN, EMOJI_MAX, len(items)))
    bad = [e for e in items if not single_emoji(e)]
    if bad:
        raise Refused("not a single emoji: %s" % ", ".join(repr(b) for b in bad))
    if len(set(items)) != len(items):
        raise Refused("q2 emoji must be distinct")
    return items


def ack_problem(ack, step, root, check, path):
    """Why --ack cannot be sent, or None.  Checked BEFORE anything is sent
    (a Stop block cannot retract a delivered message): the screen asks the
    question, so the ack asks none; no second-person form or substitute
    possessive before the address term is set (Q1-Q3); on a kit-pending
    main the Q1 ack may be the self-introduction, so it also obeys the
    opener term list (first_contact_terms.py, the gate's single source).
    A stamped role refuses a generic personal-agent introduction at Q1."""
    if "?" in ack or "\uff1f" in ack:
        return "--ack asks a question; the screen asks it -- drop the question"
    # DGN-1870: the send boundary must enforce the stamped-role branch too.
    # Check before the kit-pending term scan, which allows "personal agent".
    role = (check.primary_focus_role(path)
            if step == "q1" and check is not None and path else "")
    if step == "q1" and role and re.search(
            r"\bpersonal\s+agent\b|\uac1c\uc778\s*\uc5d0\uc774\uc804\ud2b8", ack, re.I):
        return ("--ack uses a generic personal-agent introduction; Primary focus "
                "is stamped. Rewrite it to introduce yourself using the role %s "
                "in your own words, omitting any stored owner name/title/address."
                % json.dumps(role, ensure_ascii=False))
    if _fc_terms is None or step not in ("q1", "q2", "q3"):
        return None
    found = _fc_terms.opener_violations(ack)
    if not found:
        return None
    kit_pending = False
    try:
        kit_pending = bool(path and check is not None and check.primary_focus_unset(path))
    except Exception:
        kit_pending = False
    if found["pronouns"]:
        return ("--ack addresses the user (%s) before the address term is set"
                % ", ".join(found["pronouns"]))
    if step == "q1" and kit_pending and found["terms"]:
        return ("--ack names tasks/domains/capabilities (%s); no kit is on yet"
                % ", ".join(found["terms"]))
    return None


# ------------------------------------------------------------------ render
def tone_rows(root, lang, lines, check):
    """[(label, sample)] for Q4: the installed pack's candidates (address
    token filled with the Q3 answer), else the generic four."""
    found = None
    if check is not None:
        try:
            found = check.resolve_tone_candidates({"cwd": root}, lang)
        except Exception:
            found = None
    if found:
        address = address_term(lines)
        rows = []
        for item in found[1]:
            sample = item["sample"].strip().replace("{address}", address)
            rows.append((item["label"].strip(), " ".join(sample.split())))
        return rows
    return [(copy(root, lang, "onboarding.q4.tone%d.label" % i),
             copy(root, lang, "onboarding.q4.tone%d.sample" % i))
            for i in range(1, GENERIC_TONES + 1)]


def render(step, root, emoji=None, check=None):
    """(body, option labels) of one step's screen."""
    check = check if check is not None else load_check(root)
    lang = instance_lang(root)
    path = identity_path(root, check)
    lines = _identity_lines(path) if path else []
    if step == "q1":
        return copy(root, lang, "onboarding.q1"), []
    if step == "q2":
        return copy(root, lang, "onboarding.q2"), validate_emoji(emoji)
    if step == "q3":
        found = []
        if _q3_candidates is not None:
            try:
                found = _q3_candidates(root, lang)
            except Exception:
                found = []
        if not found:
            return copy(root, lang, "onboarding.q3"), []
        return copy(root, lang, "onboarding.q3_candidates"), list(found)
    if step == "q4":
        rows = tone_rows(root, lang, lines, check)
        listing = "\n".join('%d. %s -- "%s"' % (i, label, sample)
                            for i, (label, sample) in enumerate(rows, 1))
        return copy(root, lang, "onboarding.q4") + "\n\n" + listing, \
            [label for label, _sample in rows]
    if step == "q5":
        return copy(root, lang, "onboarding.q5"), list(HUMOR_OPTIONS)
    if step == "kit":
        return copy(root, lang, "kit.offer"), [
            copy(root, lang, "onboarding.kit.option_life"),
            copy(root, lang, "onboarding.kit.option_none")]
    raise Refused("unknown step %s" % step)


def button_rows(options):
    """Inline keyboard rows in the bridge's own shape (mint_screens.
    button_rows): "N. label", callback "opt:N. label", "opt:N" past 64
    bytes; the bridge's callback path turns a tap into an owner message."""
    rows = []
    for i, label in enumerate(options, 1):
        text = "%d. %s" % (i, label)
        cbd = "opt:" + text
        if len(cbd.encode("utf-8")) > CALLBACK_MAX:
            cbd = "opt:%d" % i
        rows.append([{"text": text, "callback_data": cbd}])
    return rows


def fallback_text(body, options):
    """The screen as a model reply: body, numbered options, [[OPTIONS]]."""
    if not options:
        return body
    return body + "\n\n" + "\n".join(
        "%d. %s" % (i, o) for i, o in enumerate(options, 1)) + "\n[[OPTIONS]]"


# ------------------------------------------------------------------ send
def log_push_failure(root, step, rc, stderr):
    """One best-effort diagnostic line; never change the delivery outcome."""
    try:
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        line = "onboarding-screen: step=%s push rc=%s stderr=%s\n" % (
            step, rc, json.dumps((stderr or "")[-300:], ensure_ascii=True))
        log_dir = os.path.join(root, ".telegram_bot", "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "onboarding-gate.log"), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def push(root, text, buttons, push_sh=None, runner=subprocess.run, step=None):
    """One strict send -> ("ok", message_id) | ("failed", None) | ("uncertain", None)."""
    push_sh = push_sh or os.environ.get("ONBOARDING_SCREEN_PUSH") \
        or os.path.join(HERE, "push.sh")
    fd, expect = tempfile.mkstemp(prefix="onboarding-screen-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        cmd = ["bash", push_sh, "--strict", "--audience", "owner",
               "--text", text, "--expect-file", expect]
        if buttons:
            cmd += ["--buttons-json", json.dumps(buttons, ensure_ascii=False)]
        try:
            r = runner(cmd, capture_output=True, text=True, timeout=PUSH_TIMEOUT_S,
                       cwd=root)
        except subprocess.TimeoutExpired as exc:
            log_push_failure(root, step, "timeout", exc.stderr)
            return "uncertain", None
        except OSError as exc:
            log_push_failure(root, step, "not-started", str(exc))
            return "failed", None
    finally:
        try:
            os.unlink(expect)
        except OSError:
            pass
    if r.returncode != 0:
        log_push_failure(root, step, r.returncode, r.stderr)
        if r.returncode == 6:
            return "uncertain", None
        # rc 2 includes connect-phase failures: the model must send owner_text.
        sys.stderr.write("onboarding-screen: push rc=%s %s\n"
                         % (r.returncode, (r.stderr or "").strip()[-300:]))
        return "failed", None
    try:
        rec = json.loads((r.stdout or "").strip().splitlines()[-1])
        mid = rec["message_id"]
        if rec.get("ok") is True and isinstance(mid, int) and not isinstance(mid, bool):
            return "ok", mid
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    log_push_failure(root, step, r.returncode, r.stderr)
    return "uncertain", None


def result(step, note, delivered=False, **extra):
    out = {"step": step, "screen_delivered": delivered, "note_for_agent": note,
           "agent_utterance": extra.pop("agent_utterance", "none")}
    out.update(extra)
    return out


def run(step, root, emoji=None, ack=None, dry_run=False, push_sh=None,
        runner=subprocess.run):
    check = load_check(root)
    due = due_step(root, check)
    if step == "next":
        step = due
        if step is None:
            return result(None, NOTE_REFUSED, error="no onboarding screen is due",
                          due_step=None)
    if step not in STEPS:
        return result(step, NOTE_REFUSED, error="unknown step", due_step=due)
    if step != due:
        return result(step, NOTE_REFUSED, due_step=due, error=(
            "step %s is not due (due: %s). Record the owner's answer into the "
            "identity file first, then call the due step." % (step, due or "none")))
    if ack is not None:
        ack = ack.strip()
        if len(ack) > ACK_MAX or "[[" in ack:
            return result(step, NOTE_REFUSED, due_step=due,
                          error="--ack must be one short plain reply (<= %d chars)" % ACK_MAX)
    if ack:
        problem = ack_problem(ack, step, root, check, identity_path(root, check))
        if problem:
            return result(step, NOTE_REFUSED, due_step=due, error=problem)
    try:
        body, options = render(step, root, emoji, check)
    except Refused as exc:
        return result(step, NOTE_REFUSED, due_step=due, error=str(exc))
    text = ack + "\n\n" + body if ack else body
    buttons = button_rows(options)
    if dry_run:
        return result(step, NOTE_DRY, sent_text=[text],
                      options=options, buttons=[buttons])
    kind, mid = push(root, text, buttons, push_sh, runner, step=step)
    if kind == "uncertain":
        return result(step, NOTE_UNCERTAIN, sent_text=[text], options=options)
    if kind != "ok":
        return result(step, NOTE_FAILED, agent_utterance="fallback",
                      sent_text=[], owner_text=fallback_text(text, options))
    if _fc_state is not None:
        try:
            _fc_state.record_screen(_fc_state.resolve_log_dir(root), step, mid)
        except Exception:
            pass
    return result(step, NOTE_SENT, delivered=True, sent_text=[text],
                  options=options, message_id=mid)


# ------------------------------------------------------------------ opener
def opener_due(root, check=None):
    """The bridge sends the fixed opener (no model turn) iff onboarding is
    pending on a kit-pending instance (Primary focus still the placeholder);
    a domain-role instance keeps its model-composed opener."""
    check = check if check is not None else load_check(root)
    path = identity_path(root, check)
    if not path or check is None:
        return False
    try:
        return bool(check.needs_onboarding(path) and check.primary_focus_unset(path))
    except Exception:
        return False


def opener_text(root):
    """The fixed opener copy, or '' when it cannot be read."""
    try:
        return copy(root, instance_lang(root), "onboarding.opener")
    except Refused:
        return ""


def main(argv):
    args = list(argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stderr.write(__doc__.split("Usage:", 1)[1].split("English", 1)[0])
        return 2
    step = args.pop(0)
    emoji, ack, root, dry = [], None, os.path.dirname(HERE), False
    while args:
        a = args.pop(0)
        if a == "--emoji" and args:
            emoji.append(args.pop(0))
        elif a == "--ack" and args:
            ack = args.pop(0)
        elif a == "--root" and args:
            root = args.pop(0)
        elif a == "--dry-run":
            dry = True
        else:
            sys.stderr.write("onboarding-screen: unknown argument %r\n" % a)
            return 2
    try:
        out = run(step, os.path.abspath(os.path.expanduser(root)), emoji, ack, dry)
    except Exception as exc:                 # never a traceback to the model
        out = result(step, NOTE_REFUSED, error="internal error: %s" % type(exc).__name__)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
