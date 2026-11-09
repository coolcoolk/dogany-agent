"""One owner-facing job block (DGN-2229, owner format v4 + v3 default hiding).

The background start notice, the end notice and the workbench background
section all render the same block per job:

    <title>
    <bullet> <actor kind>: <model>[ (<tag>)], HH:MM[ (<elapsed>)]

A script on this device has no model: "<bullet> <actor kind>, HH:MM".

The tag is shown only when something is NOT the default (this device,
subscription billing):
  * nothing            this device + subscription
  * cloud              any cloud on subscription
  * cloud credit       a cloud run billed to credit
  * api credit         a local claude-api run
  * <ComputerName>     another device, read from the run record

NO COPY HERE.  The actor-kind words and the tag words are catalog keys
(config/i18n/<lang>.json: term.subagent, term.script, run.tag.*), read
through lib/i18n_lookup.py like every other owner string; the runtime ->
(place, billing) mapping is the one data table RUNTIME_PLACE and the
(place, billing) -> catalog key mapping is the one data table TAG_KEY. A new
runtime is one new row plus catalog keys, no logic change.  The device name
is read at render time (scutil ComputerName on macOS, hostname elsewhere).

BILLING NOTE.  A run record carries the runtime, not the plan-versus-credit
split of a cloud run.  RUNTIME_PLACE therefore holds the default billing of
each runtime and a caller that KNOWS better passes billing= (from a record);
nothing here guesses it.

CLI (the bash notice rails shell out to this, like model_display.py):
  run_block.py [--quote] [--kind subagent|script] [--model LABEL]
               [--runtime RT] [--billing subscription|credit] [--host NAME]
               [--clock HH:MM] [--elapsed TEXT] --title TITLE
"""
import html
import json
import os
import re
import socket
import subprocess
import sys

# The framework checkout exposes routines/ as a symlink to agents/.template;
# catalogs belong to that template, just as they belong to an installed root.
LIB_DIR = os.path.dirname(os.path.realpath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(LIB_DIR))
sys.path.insert(0, LIB_DIR)

import i18n_lookup  # noqa: E402
try:  # sandboxes/kits that ship run_block + i18n_lookup only keep the catalog path
    import term_registry  # noqa: E402
except Exception:  # pragma: no cover - fail-open: ko/en catalog words still render
    term_registry = None

BULLET = "\u2022"
LOCAL, CLOUD = "local", "cloud"
SUBSCRIPTION, CREDIT = "subscription", "credit"

# runtime -> (place, default billing)
RUNTIME_PLACE = {
    "claude-code": (LOCAL, SUBSCRIPTION),
    "codex": (LOCAL, SUBSCRIPTION),
    "claude-api": (LOCAL, CREDIT),
}

# (place, billing) -> catalog key of the tag; None = the default, no tag
TAG_KEY = {
    (LOCAL, SUBSCRIPTION): None,
    (LOCAL, CREDIT): "run.tag.api_credit",
    (CLOUD, SUBSCRIPTION): "run.tag.cloud",
    (CLOUD, CREDIT): "run.tag.cloud_credit",
}

KIND_KEY = {"subagent": "term.subagent", "script": "term.script"}
KIND_TERM = {"subagent": "subagent", "script": "script"}


def owner_lang():
    return os.environ.get("AGENT_LANG") or "ko"


def word(key, root=None, lang=None):
    """Catalog string for key, "" when no layer has it."""
    try:
        return i18n_lookup.lookup(root or ROOT_DIR, lang or owner_lang(), key)
    except Exception:
        return ""


def effective_actor_word(kind, root=None, lang=None):
    """Effective owner term for an actor kind.

    Registered terms use the complete user-term ladder, including the
    pre-DGN-1980 agent.conf compatibility knob.  Unregistered actor words
    still use the shared layered i18n lookup.
    """
    base = root or ROOT_DIR
    language = lang or owner_lang()
    try:
        out = term_registry.resolve_term(base, language, KIND_TERM[kind])
    except Exception:
        out = ""
    return out or word(KIND_KEY[kind], base, language) or kind


def local_host():
    """This device's name, read now: ComputerName on macOS, hostname else.
    DOGANY_RUN_BLOCK_HOST overrides it (test seam: tests inject a fake
    name instead of naming a machine)."""
    forced = os.environ.get("DOGANY_RUN_BLOCK_HOST")
    if forced:
        return forced
    try:
        out = subprocess.run(["scutil", "--get", "ComputerName"],
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        return socket.gethostname() or ""
    except Exception:
        return ""


def tag(runtime, billing=None, host=None, root=None, lang=None):
    """The non-default tag of one run, "" when everything is default.

    host (the device the run record names) wins when it differs from this
    device; an unknown runtime shows no tag rather than a guess."""
    h = (host or "").strip()
    if h and h != local_host():
        return h
    row = RUNTIME_PLACE.get(runtime or "")
    if row is None:
        return ""
    key = TAG_KEY.get((row[0], billing or row[1]))
    return word(key, root, lang) if key else ""


def actor(kind, model="", tag_text="", root=None, lang=None):
    """"<actor kind>[: <model>[ (<tag>)]]" -- no model, no colon."""
    out = effective_actor_word(kind, root, lang)
    if model:
        suffix = " (%s)" % tag_text if tag_text else ""
        # a display label may already carry the tag (e.g. "Codex (Cloud)")
        out += ": " + model + ("" if suffix and model.endswith(suffix) else suffix)
    return out


def job_lines(title, kind, model="", tag_text="", clock="", elapsed="",
              root=None, lang=None):
    """The two lines of one job block."""
    line = BULLET + " " + actor(kind, model, tag_text, root, lang)
    if clock:
        line += ", " + clock + (" (%s)" % elapsed if elapsed else "")
    return [title, line]


TICKET_TAIL = re.compile(r"^(.*\S)\s+(\[[^\[\]\s]+\])\s*$")


def board_job_lines(name, kind, model="", tag_text="", stamp="",
                    actor_word="", root=None, lang=None):
    """The two lines of one workbench job block (dec-582):

        [<ticket>] <name> <HH:MM> (<N>m~)
        <bullet> <actor>: <model>[ <place tag>]

    A trailing "[ticket]" of the name moves to the front; no ticket, no
    bracket; no recorded start (empty stamp), no time.  The time lives on
    line 1 only.  actor_word (the instance sibling term) replaces the
    catalog word of a dispatched sibling job; a script keeps its own word.
    A job with no model prints the bare actor word."""
    title = name
    m = TICKET_TAIL.match(name or "")
    if m:
        title = m.group(2) + " " + m.group(1)
    if stamp:
        title += " " + stamp
    who = effective_actor_word(kind, root, lang)
    if kind == "subagent" and actor_word:
        who = actor_word
    line = BULLET + " " + who
    if model:
        line += ": " + model + (" (%s)" % tag_text if tag_text else "")
    return [title, line]


def quote(lines):
    """One job block wrapped in its own blockquote (notices)."""
    return "<blockquote>" + "\n".join(
        html.escape(x, quote=False) for x in lines) + "</blockquote>"


def main(argv):
    opts = {"--kind": "subagent", "--model": "", "--runtime": "",
            "--billing": "", "--host": "", "--clock": "", "--elapsed": "",
            "--title": ""}
    want_quote = False
    args = list(argv[1:])
    while args:
        a = args.pop(0)
        if a == "--quote":
            want_quote = True
        elif a in opts and args:
            opts[a] = args.pop(0)
    if opts["--kind"] not in KIND_KEY:
        return 0
    t = tag(opts["--runtime"], opts["--billing"] or None, opts["--host"] or None) \
        if opts["--model"] else ""
    lines = job_lines(opts["--title"], opts["--kind"], opts["--model"], t,
                      opts["--clock"], opts["--elapsed"])
    sys.stdout.write(quote(lines) if want_quote else "\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
