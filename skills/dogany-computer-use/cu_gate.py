#!/usr/bin/env python3
"""cu_gate.py -- dogany-computer-use consequence gate (DGN-1972).

Rule-lane carrier for three steps of the skill: classify, receipt, lease.
Pure stdlib, ASCII source (non-ASCII labels are written as \\u escapes).

Verbs (all print one JSON object on stdout):

  route <task-json>
      plan the dec-366 model route without spawning or touching the screen.
      Task: {apps: [bundle-id, ...], short: bool, urgent: bool,
             previous: {model: haiku|sonnet,
                        outcome: confirmed|failed|uncertain|blocked|cancelled}}
      previous is optional; failed means verified not applied, never timeout.
      Worker routes are launch_ready=false until the dispatch sandbox pilot.
      exit 0 plan | 2 bad input | 14 switched off. No state writes.
  classify <action-json>
      verdict allow | gate | deny + reasons.
      exit 0 allow | 10 gate | 12 deny | 2 bad input.
  request --action <json> --observation <json> [--ttl S]
      open a pending approval bound to the action + observed state.
      prints id. exit 0.
  approve <id> [--source options|instruction]
      owner said yes -> receipt becomes usable once. exit 0 | 11 refused.
  consume <id> --action <json> --observation <json>
      spend the receipt right before the ONE execution. refused when
      missing / not approved / already used / expired / action differs /
      state differs. A state change also INVALIDATES the receipt for good.
      exit 0 ok | 11 refused.
  result <id> --outcome confirmed|uncertain|failed
      record what the post-action observation showed. exit 0.
  lease acquire|renew|release|status --holder <name> [--ttl S] [--desktop K]
      per-desktop mutation lease. exit 0 ok | 13 held by another | 2 usage
      | 14 (acquire) switched off by the owner: COMPUTER_USE=off in
      config/agent.conf (DGN-1976 preview switch, routines/preview-switch.py).

Action descriptor (built by the agent from the fresh observation):
  {"tool": "click", "app": "<bundle id>",
   "target": {"label": "...", "role": "AXButton"},
   "focus_role": "AXTextField",          # focused element role, if known
   "key": "return", "keys": ["cmd", "shift", "delete"],   # press_key or hotkey
   "text": "...", "menu_path": ["File", "..."],   # or "File > ..."
   "drop_target": {"label": "..."},
   "dialog": {"present": true, "default_button": "...", "buttons": [...]},
   "actions": [<descriptor>, ...]}      # run_actions batch
Every menu_path item is matched by the label rules, on any tool that
carries one. App-aware harmless labels / keys (HARMLESS) only lift a
label or delete-key gate in a listed app with no dialog up; unknown apps
stay default-deny.
Observation (state binding): any JSON object; the keys in STATE_KEYS are
hashed (session included: element tokens are per driver session). Missing keys hash as null, so the agent must pass the same shape.

State home: $DOGANY_CU_HOME, else ~/.dogany/computer-use (machine-wide:
the desktop is shared by every instance on the host).
"""

import fcntl
import hashlib
import json
import os
import re
import socket
import sys
import time
import unicodedata
import uuid

# ---------------------------------------------------------------- vocabulary

# Consequential button / menu labels. en matched on word boundaries, ko as
# substrings (no word boundaries in Hangul labels). A hit gates; it never
# allows. Keyword lists are a floor, not the whole gate: unknown -> gate.
LABELS_EN = [
    "delete", "delete permanently", "remove", "erase", "empty trash",
    "empty", "move to trash", "move to bin", "send", "send now", "submit",
    "pay", "pay now", "buy", "purchase", "place order", "order", "checkout",
    "check out", "confirm payment", "transfer", "donate", "subscribe",
    "unsubscribe", "publish", "post", "share", "replace", "overwrite",
    "don't save", "dont save", "discard", "revert", "reset", "format",
    "uninstall", "sign out", "log out", "deactivate", "close account",
    "allow", "always allow", "grant", "install", "update now", "restart",
    "shut down", "force quit", "approve", "accept", "merge",
]
LABELS_KO = [
    "\uc0ad\uc81c",                         # sakje (delete)
    "\uc601\uad6c \uc0ad\uc81c",            # permanent delete
    "\uc9c0\uc6b0\uae30",                   # erase
    "\uc81c\uac70",                         # remove
    "\ud734\uc9c0\ud1b5\uc73c\ub85c \uc774\ub3d9", # move to trash
    "\ud734\uc9c0\ud1b5 \ube44\uc6b0\uae30", # empty trash
    "\ube44\uc6b0\uae30",                   # empty
    "\ubcf4\ub0b4\uae30",                   # send
    "\uc804\uc1a1",                         # transmit
    "\ubc1c\uc1a1",                         # dispatch
    "\uc81c\ucd9c",                         # submit
    "\uacb0\uc81c",                         # payment
    "\uad6c\ub9e4",                         # purchase
    "\uc8fc\ubb38",                         # order
    "\uc1a1\uae08",                         # remit
    "\uc774\uccb4",                         # bank transfer
    "\ud6c4\uc6d0",                         # donate
    "\uad6c\ub3c5",                         # subscribe
    "\uac8c\uc2dc",                         # publish / post
    "\uc62c\ub9ac\uae30",                   # upload / post
    "\uacf5\uc720",                         # share
    "\ub300\uce58",                         # replace (Finder)
    "\ub36e\uc5b4\uc4f0\uae30",             # overwrite
    "\uc800\uc7a5 \uc548 \ud568",           # don't save
    "\uc800\uc7a5\ud558\uc9c0 \uc54a\uc74c", # don't save
    "\ubc84\ub9ac\uae30",                   # discard
    "\ub418\ub3cc\ub9ac\uae30",             # revert
    "\ucd08\uae30\ud654",                   # reset
    "\uc7ac\uc124\uc815",                   # reset
    "\ud3ec\ub9f7",                         # format
    "\uc124\uce58 \uc81c\uac70",            # uninstall
    "\ub85c\uadf8\uc544\uc6c3",             # log out
    "\ud0c8\ud1f4",                         # leave / close account
    "\ud5c8\uc6a9",                         # allow
    "\uc124\uce58",                         # install
    "\uc5c5\ub370\uc774\ud2b8",             # update
    "\uc7ac\uc2dc\ub3d9",                   # restart
    "\ub2e4\uc2dc \uc2dc\uc791",            # restart
    "\uc885\ub8cc",                         # shut down / quit
    "\uc2b9\uc778",                         # approve
    "\uc218\ub77d",                         # accept
    "\ubcd1\ud569",                         # merge
]
TRASH_TARGETS_EN = ["trash", "bin", "recycle bin"]
TRASH_TARGETS_KO = ["\ud734\uc9c0\ud1b5"]

# Destructive shortcuts. Key = sorted modifiers + key after normalize_combo.
SHORTCUTS = {
    "cmd+delete": "move to trash",
    "cmd+shift+delete": "empty trash",
    "alt+cmd+shift+delete": "empty trash without confirm",
    "alt+cmd+delete": "delete immediately",
    "cmd+return": "send / submit",
    "ctrl+return": "send / submit",
    "cmd+shift+d": "send (mail)",
    "alt+cmd+escape": "force quit",
    "shift+delete": "permanent delete",
    "alt+ctrl+delete": "system interrupt",
}
# Shortcuts that are consequential only while a dialog is up.
DIALOG_SHORTCUTS = {
    "cmd+d": "don't save",
    "cmd+delete": "don't save / delete",
}

MOD_ALIASES = {
    "command": "cmd", "cmd": "cmd", "meta": "cmd", "super": "cmd",
    "win": "cmd", "option": "alt", "opt": "alt", "alt": "alt",
    "control": "ctrl", "ctrl": "ctrl", "shift": "shift",
}
KEY_ALIASES = {
    "backspace": "delete", "delete": "delete", "del": "fdelete",
    "forward_delete": "fdelete", "forwarddelete": "fdelete",
    "return": "return", "enter": "return", "kp_enter": "return",
    "esc": "escape", "escape": "escape",
}
TEXT_ROLES = {
    "axtextfield", "axtextarea", "axcombobox", "axsearchfield",
    "axsecuretextfield", "text", "entry", "edit", "textbox",
}
SEND_ON_RETURN_APPS = {
    "com.apple.mobilesms", "com.apple.mail", "com.kakao.kakaotalkmac",
    "com.tinyspeck.slackmacgap", "ru.keepcoder.telegram",
    "org.telegram.desktop", "com.hnc.discord", "net.whatsapp.whatsapp",
    "com.microsoft.teams", "com.microsoft.teams2", "com.microsoft.outlook",
}

# Tool classes (cua-driver MCP tool names, 0.34.x).
OBSERVE_TOOLS = {
    "list_apps", "list_windows", "get_window_state", "get_accessibility_tree",
    "verify_state", "parse_visual_regions", "get_desktop_state",
    "get_screen_size", "get_cursor_position", "zoom", "start_session",
    "end_session", "get_session", "list_sessions", "check_permissions",
    "health_report", "get_agent_cursor_state", "get_recording_state",
    "get_config", "check_for_update", "get_browser_state",
}
BENIGN_INPUT_TOOLS = {
    "scroll", "move_cursor", "right_click", "launch_app", "bring_to_front",
    "set_window_frame", "set_agent_cursor_enabled", "set_agent_cursor_motion",
    "set_agent_cursor_theme", "browser_prepare", "browser_navigate",
}
LABEL_TOOLS = {"click", "double_click", "browser_click", "set_value",
               "browser_pointer"}
ALWAYS_GATE_TOOLS = {
    "kill_app": "terminates a process",
    "clipboard_read": "reads possibly private clipboard",
    "clipboard_write": "replaces clipboard",
    "start_recording": "records the screen",
    "browser_download": "writes a file",
    "browser_set_input_files": "uploads a local file",
    "browser_dialog": "resolves a page dialog",
}
DENY_TOOLS = {
    "set_config": "changes driver policy",
    "install_extension": "installs driver code",
    "install_ffmpeg": "installs a binary",
    "replay_trajectory": "replays recorded input blind",
    "escalate_session": "legacy scope escalation",
    "page": "legacy page mutation",
}

# App-aware harmless in-app labels / keys: a label or delete-key hit in
# these apps is lifted only when the label matches exactly (normalized) and
# no dialog is up. Not listed -> default rules (gate). Bundle ids lower.
HARMLESS = {
    "com.apple.calculator": {
        "labels": {
            "\uc0ad\uc81c",                   # sakje (backspace key)
            "\ubaa8\ub450 \uc9c0\uc6b0\uae30",   # all clear
            "\uc9c0\uc6b0\uae30",             # clear
            "delete", "clear", "all clear", "ac", "c",
        },
        "keys": {"delete", "fdelete", "escape"},
    },
}

STATE_KEYS = ("app", "pid", "window_id", "snapshot_id", "dialog", "target",
              "focus_role", "url", "session")

APPROVAL_TTL = 300
LEASE_TTL = 120

EXIT_ALLOW, EXIT_GATE, EXIT_DENY = 0, 10, 12
EXIT_REFUSED, EXIT_HELD, EXIT_USAGE = 11, 13, 2
EXIT_OFF = 14     # DGN-1976: COMPUTER_USE=off


# ---------------------------------------------------------------- classifier

def norm_label(s):
    s = unicodedata.normalize("NFC", str(s or ""))
    s = re.sub(r"(\s*(\u2026|\.{2,}))+\s*$", "", s)   # trailing ellipsis
    s = s.replace("\u2026", " ").replace("...", " ").replace("&", "")
    s = s.replace("\u2019", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def label_hits(label):
    lab = norm_label(label)
    if not lab:
        return []
    hits = []
    for term in LABELS_EN:
        if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", lab):
            hits.append(term)
    for term in LABELS_KO:
        if term in lab:
            hits.append(term)
    return hits


def is_trash_target(label):
    lab = norm_label(label)
    if not lab:
        return False
    if any(t in lab for t in TRASH_TARGETS_KO):
        return True
    return any(re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", lab)
               for t in TRASH_TARGETS_EN)


def normalize_combo(keys):
    mods, main = set(), []
    for k in keys or []:
        k = str(k).strip().lower()
        if k in MOD_ALIASES:
            mods.add(MOD_ALIASES[k])
        else:
            main.append(KEY_ALIASES.get(k, k))
    return "+".join(sorted(mods) + main)


def menu_items(path):
    """menu_path as a list or one 'A > B' string -> list of items."""
    if isinstance(path, str):
        path = re.split(r"\s*(?:>|\u203a|\u25b8|->)\s*", path)
    if not isinstance(path, list):
        return []
    return [str(x) for x in path if str(x or "").strip()]


def _harmless(a):
    if _dialog(a) is not None:
        return None
    return HARMLESS.get(str(a.get("app") or "").strip().lower())


def harmless_label(a, label):
    h = _harmless(a)
    return h is not None and norm_label(label) in h["labels"]


def harmless_key(a, key):
    h = _harmless(a)
    return h is not None and key in h["keys"]


def _combo_keys(a):
    """keys list (or 'cmd+shift+delete' string) from key / keys."""
    keys = a.get("keys")
    if isinstance(keys, str):
        keys = re.split(r"\s*\+\s*", keys)
    if not isinstance(keys, list) or not keys:
        k = a.get("key")
        if not k:
            return []
        keys = re.split(r"(?<=.)\+", str(k))
    return [str(x) for x in keys if str(x or "").strip()]


def _combo_reasons(a, combo):
    if combo in SHORTCUTS:
        return ["shortcut %s (%s)" % (combo, SHORTCUTS[combo])]
    if _dialog(a) is not None and combo in DIALOG_SHORTCUTS:
        return ["shortcut %s in dialog (%s)" % (combo, DIALOG_SHORTCUTS[combo])]
    if "+" not in combo:
        return _key_reasons(a, combo)
    return []


def _dialog(a):
    d = a.get("dialog") or {}
    return d if d.get("present") else None


def _return_reasons(a):
    """Return / Enter: activates a dialog default button or sends."""
    d = _dialog(a)
    if d is not None:
        default = d.get("default_button")
        if not default:
            return ["return with dialog up, default button unknown"]
        hits = label_hits(default)
        if hits:
            return ["return activates default button '%s' (%s)"
                    % (default, ", ".join(hits))]
        return []
    app = str(a.get("app") or "").lower()
    if app in SEND_ON_RETURN_APPS:
        return ["return in a messaging app sends"]
    return []


def _key_reasons(a, key):
    key = KEY_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
    if key == "return":
        return _return_reasons(a)
    if key in ("delete", "fdelete"):
        if harmless_key(a, key):
            return []
        role = str(a.get("focus_role") or "").lower()
        if role not in TEXT_ROLES:
            return ["delete key outside a text field (role '%s')"
                    % (a.get("focus_role") or "unknown")]
    return []


def classify(a):
    """-> (verdict, reasons). verdict in allow | gate | deny."""
    if not isinstance(a, dict):
        return "gate", ["descriptor is not an object"]
    tool = str(a.get("tool") or "").strip()
    if not tool:
        return "gate", ["no tool named"]

    if tool == "run_actions":
        subs = a.get("actions")
        if not isinstance(subs, list) or not subs:
            return "gate", ["batch without readable actions"]
        verdict, reasons = "allow", []
        for i, sub in enumerate(subs):
            v, r = classify(sub)
            reasons += ["[%d] %s" % (i, x) for x in r]
            if v == "deny":
                verdict = "deny"
            elif v == "gate" and verdict == "allow":
                verdict = "gate"
        return verdict, reasons

    if tool in DENY_TOOLS:
        return "deny", ["%s: %s" % (tool, DENY_TOOLS[tool])]
    if tool in ALWAYS_GATE_TOOLS:
        return "gate", ["%s: %s" % (tool, ALWAYS_GATE_TOOLS[tool])]
    if tool in OBSERVE_TOOLS or tool in BENIGN_INPUT_TOOLS:
        return "allow", []

    reasons = []
    target = a.get("target") or {}
    menu = menu_items(a.get("menu_path"))
    for item in menu:
        hits = label_hits(item)
        if hits and not harmless_label(a, item):
            reasons.append("menu item '%s' (%s)" % (item, ", ".join(hits)))
    if tool in LABEL_TOOLS:
        label = target.get("label")
        if not label and not target.get("role") and not menu:
            return "gate", ["%s target unresolved (no AX label/role)" % tool]
        hits = label_hits(label)
        if hits and not harmless_label(a, label):
            reasons.append("label '%s' (%s)" % (label, ", ".join(hits)))
    elif tool == "invoke_menu":
        if not menu:
            return "gate", ["menu path missing"]
    elif tool == "drag":
        drop = (a.get("drop_target") or {}).get("label")
        if not drop:
            return "gate", ["drag drop target unresolved"]
        if is_trash_target(drop):
            reasons.append("drag onto trash '%s'" % drop)
        hits = label_hits(drop)
        if hits:
            reasons.append("drop target '%s' (%s)" % (drop, ", ".join(hits)))
    elif tool in ("press_key", "hotkey"):
        combo = normalize_combo(_combo_keys(a))
        if not combo:
            return "gate", ["%s without key" % tool]
        reasons += _combo_reasons(a, combo)
    elif tool in ("type_text", "browser_type"):
        if re.search(r"[\r\n]", str(a.get("text") or "")):
            r = _return_reasons(a)
            if r:
                reasons += ["text carries newline"] + r
    else:
        return "gate", ["unknown tool '%s' (fail closed)" % tool]

    return ("gate" if reasons else "allow"), reasons


# ---------------------------------------------------------------- store

def home():
    h = os.environ.get("DOGANY_CU_HOME") or os.path.join(
        os.path.expanduser("~"), ".dogany", "computer-use")
    os.makedirs(os.path.join(h, "receipts"), exist_ok=True)
    os.makedirs(os.path.join(h, "leases"), exist_ok=True)
    return h


class Locked(object):
    def __init__(self, h):
        self.path = os.path.join(h, ".lock")

    def __enter__(self):
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def now():
    return float(os.environ.get("DOGANY_CU_NOW") or time.time())


def canon_hash(obj):
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def state_hash(obs):
    obs = obs if isinstance(obs, dict) else {}
    return canon_hash({k: obs.get(k) for k in STATE_KEYS})


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, sort_keys=True, indent=1)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def receipt_path(h, rid):
    if not re.match(r"^[0-9a-f]{32}$", rid or ""):
        return None
    return os.path.join(h, "receipts", rid + ".json")


# ---------------------------------------------------------------- receipts

def cmd_request(action, obs, ttl):
    verdict, reasons = classify(action)
    if verdict == "deny":
        return {"ok": False, "error": "denied", "reasons": reasons}, EXIT_DENY
    h = home()
    rid = uuid.uuid4().hex
    rec = {
        "id": rid, "status": "pending", "created": now(),
        "expires": now() + ttl, "action_hash": canon_hash(action),
        "state_hash": state_hash(obs), "verdict": verdict,
        "reasons": reasons, "source": None,
    }
    with Locked(h):
        write_json(receipt_path(h, rid), rec)
    return {"ok": True, "id": rid, "verdict": verdict,
            "reasons": reasons, "expires": rec["expires"]}, 0


def _load(h, rid):
    p = receipt_path(h, rid)
    rec = read_json(p) if p else None
    return p, rec


def cmd_approve(rid, source):
    h = home()
    with Locked(h):
        p, rec = _load(h, rid)
        if rec is None:
            return {"ok": False, "error": "no such receipt"}, EXIT_REFUSED
        if rec["status"] != "pending":
            return {"ok": False, "error": "receipt is %s" % rec["status"]}, \
                EXIT_REFUSED
        if now() > rec["expires"]:
            rec["status"] = "expired"
            write_json(p, rec)
            return {"ok": False, "error": "expired"}, EXIT_REFUSED
        rec["status"] = "approved"
        rec["source"] = source
        rec["approved_at"] = now()
        write_json(p, rec)
    return {"ok": True, "id": rid, "status": "approved"}, 0


def cmd_consume(rid, action, obs):
    h = home()
    with Locked(h):
        p, rec = _load(h, rid)
        if rec is None:
            return {"ok": False, "error": "no such receipt"}, EXIT_REFUSED
        st = rec["status"]
        if st != "approved":
            return {"ok": False, "error": "receipt is %s" % st}, EXIT_REFUSED
        if now() > rec["expires"]:
            rec["status"] = "expired"
            write_json(p, rec)
            return {"ok": False, "error": "expired"}, EXIT_REFUSED
        if canon_hash(action) != rec["action_hash"]:
            rec["status"] = "invalidated"
            rec["invalid_reason"] = "action changed"
            write_json(p, rec)
            return {"ok": False, "error": "action changed; ask again"}, \
                EXIT_REFUSED
        if state_hash(obs) != rec["state_hash"]:
            rec["status"] = "invalidated"
            rec["invalid_reason"] = "screen state changed"
            write_json(p, rec)
            return {"ok": False,
                    "error": "screen state changed; re-observe and ask again"}, \
                EXIT_REFUSED
        rec["status"] = "consumed"
        rec["consumed_at"] = now()
        write_json(p, rec)
    return {"ok": True, "id": rid, "status": "consumed"}, 0


def cmd_result(rid, outcome):
    h = home()
    with Locked(h):
        p, rec = _load(h, rid)
        if rec is None:
            return {"ok": False, "error": "no such receipt"}, EXIT_REFUSED
        if rec["status"] != "consumed":
            return {"ok": False, "error": "receipt is %s" % rec["status"]}, \
                EXIT_REFUSED
        rec["outcome"] = outcome
        write_json(p, rec)
    note = {"confirmed": "done",
            "uncertain": "outcome unknown: observe, never replay; new "
                         "approval needed for any retry",
            "failed": "not applied: re-observe; new approval needed"}[outcome]
    return {"ok": True, "id": rid, "outcome": outcome, "next": note}, 0


# ---------------------------------------------------------------- lease

def default_desktop():
    disp = os.environ.get("DISPLAY") or "console"
    return "%s-%d-%s" % (socket.gethostname().split(".")[0], os.getuid(),
                         re.sub(r"[^A-Za-z0-9]", "_", disp))


SWITCH_KEY = "COMPUTER_USE"


def _conf_path():
    """The instance conf: $DOGANY_AGENT_CONF, else the project root the
    runtime named, else <ROOT>/config/agent.conf from this skill folder
    (<ROOT>/.claude/skills/dogany-computer-use)."""
    env = os.environ.get("DOGANY_AGENT_CONF", "").strip()
    if env:
        return env
    root = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if root and os.path.isfile(os.path.join(root, "config", "agent.conf")):
        return os.path.join(root, "config", "agent.conf")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "..", "config",
                                         "agent.conf"))


def switched_off():
    """DGN-1976: True only for an explicit COMPUTER_USE=off (unset keeps
    the pre-switch behaviour). First active line wins, like conf_get."""
    try:
        with open(_conf_path(), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(SWITCH_KEY + "="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return v.lower() == "off"
    except OSError:
        pass
    return False


def cmd_lease(op, holder, ttl, desktop):
    h = home()
    desk = re.sub(r"[^A-Za-z0-9_.-]", "_", desktop or default_desktop())
    p = os.path.join(h, "leases", desk + ".json")
    with Locked(h):
        cur = read_json(p)
        live = cur is not None and now() < cur.get("expires", 0)
        if op == "status":
            return {"ok": True, "desktop": desk,
                    "holder": cur.get("holder") if live else None,
                    "expires": cur.get("expires") if live else None}, 0
        if not holder:
            return {"ok": False, "error": "--holder required"}, EXIT_USAGE
        mine = live and cur.get("holder") == holder
        if op == "acquire":
            if switched_off():
                return {"ok": False, "error": "switched_off",
                        "key": SWITCH_KEY}, EXIT_OFF
            if live and not mine:
                return {"ok": False, "error": "held", "desktop": desk,
                        "holder": cur.get("holder"),
                        "expires": cur.get("expires")}, EXIT_HELD
            rec = {"holder": holder, "acquired": now(),
                   "expires": now() + ttl}
            write_json(p, rec)
            return {"ok": True, "desktop": desk, "holder": holder,
                    "expires": rec["expires"]}, 0
        if op == "renew":
            if not mine:
                return {"ok": False, "error": "not holder", "desktop": desk,
                        "holder": cur.get("holder") if live else None}, \
                    EXIT_HELD
            cur["expires"] = now() + ttl
            write_json(p, cur)
            return {"ok": True, "desktop": desk, "holder": holder,
                    "expires": cur["expires"]}, 0
        if op == "release":
            if live and not mine:
                return {"ok": False, "error": "not holder", "desktop": desk,
                        "holder": cur.get("holder")}, EXIT_HELD
            if cur is not None:
                os.remove(p)
            return {"ok": True, "desktop": desk, "released": True}, 0
    return {"ok": False, "error": "unknown lease op"}, EXIT_USAGE


# ---------------------------------------------------------------- routing

def cmd_route(task):
    """dec-366 selection only; never mistake a retry for action approval.

    The observe worker cannot currently write the shared lease store. Keep
    launch readiness explicit instead of offering an unconfined fallback or
    relocating state into per-worker scratch (which would break exclusion).
    """
    if not isinstance(task, dict) or set(task) - {
            "apps", "short", "urgent", "previous"}:
        return {"ok": False, "error": "bad task fields"}, EXIT_USAGE
    apps = task.get("apps")
    if not isinstance(apps, list) or not apps or any(
            not isinstance(app, str) or not app.strip() for app in apps):
        return {"ok": False, "error": "nonempty apps list required"}, EXIT_USAGE
    if any(type(task.get(key, False)) is not bool for key in ("short", "urgent")):
        return {"ok": False, "error": "short and urgent must be booleans"}, EXIT_USAGE
    previous = task.get("previous")
    if "previous" in task and (
            not isinstance(previous, dict)
            or set(previous) != {"model", "outcome"}
            or previous["model"] not in ("haiku", "sonnet")
            or previous["outcome"] not in (
                "confirmed", "failed", "uncertain", "blocked", "cancelled")):
        return {"ok": False, "error": "bad previous attempt"}, EXIT_USAGE
    if switched_off():
        return {"ok": False, "error": "switched_off", "key": SWITCH_KEY}, EXIT_OFF

    plan = {"ok": True, "route": "stop", "model": None,
            "launch_ready": False, "approval_inherited": False,
            "lease_required": True, "observe_first": True}
    if previous:
        outcome = previous["outcome"]
        if outcome == "uncertain":
            plan.update(route="observe", reason="outcome unknown; never replay")
            return plan, 0
        if outcome != "failed":
            plan["reason"] = "previous attempt " + outcome
            return plan, 0
        if previous["model"] == "sonnet":
            plan["reason"] = "Sonnet failed; report blocker"
            return plan, 0
        model, reason = "sonnet", "verified Haiku failure"
    elif task.get("short", False) or task.get("urgent", False):
        plan.update(route="direct", reason="short or urgent task")
        return plan, 0
    elif len({app.strip().lower() for app in apps}) > 1:
        model, reason = "sonnet", "multi-app chain"
    else:
        model, reason = "haiku", "default computer-use worker"
    plan.update(route="worker", model=model, reason=reason,
                runtime="claude-code", blocker="dispatch_sandbox_pilot_pending")
    return plan, 0


# ---------------------------------------------------------------- cli

def _opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _json_arg(raw, what):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        raise SystemExit(_emit({"ok": False, "error": "bad %s json" % what},
                               EXIT_USAGE))


def _emit(obj, rc):
    print(json.dumps(obj, ensure_ascii=True, sort_keys=True))
    return rc


def main(argv):
    if not argv:
        return _emit({"ok": False, "error": "usage: see header"}, EXIT_USAGE)
    verb, rest = argv[0], argv[1:]
    if verb == "route":
        if len(rest) != 1:
            return _emit({"ok": False, "error": "one task json required"},
                         EXIT_USAGE)
        return _emit(*cmd_route(_json_arg(rest[0], "task")))
    if verb == "classify":
        if not rest:
            return _emit({"ok": False, "error": "action json required"},
                         EXIT_USAGE)
        v, r = classify(_json_arg(rest[0], "action"))
        rc = {"allow": EXIT_ALLOW, "gate": EXIT_GATE, "deny": EXIT_DENY}[v]
        return _emit({"verdict": v, "reasons": r}, rc)
    if verb == "request":
        action = _json_arg(_opt(rest, "--action"), "action")
        obs = _json_arg(_opt(rest, "--observation"), "observation")
        ttl = float(_opt(rest, "--ttl", APPROVAL_TTL))
        return _emit(*cmd_request(action, obs, ttl))
    if verb == "approve":
        if not rest:
            return _emit({"ok": False, "error": "id required"}, EXIT_USAGE)
        src = _opt(rest, "--source", "options")
        if src not in ("options", "instruction"):
            return _emit({"ok": False, "error": "bad --source"}, EXIT_USAGE)
        return _emit(*cmd_approve(rest[0], src))
    if verb == "consume":
        if not rest:
            return _emit({"ok": False, "error": "id required"}, EXIT_USAGE)
        action = _json_arg(_opt(rest, "--action"), "action")
        obs = _json_arg(_opt(rest, "--observation"), "observation")
        return _emit(*cmd_consume(rest[0], action, obs))
    if verb == "result":
        out = _opt(rest, "--outcome")
        if not rest or out not in ("confirmed", "uncertain", "failed"):
            return _emit({"ok": False, "error": "id + --outcome required"},
                         EXIT_USAGE)
        return _emit(*cmd_result(rest[0], out))
    if verb == "lease":
        if not rest or rest[0] not in ("acquire", "renew", "release",
                                       "status"):
            return _emit({"ok": False, "error": "lease op required"},
                         EXIT_USAGE)
        return _emit(*cmd_lease(rest[0], _opt(rest, "--holder"),
                                float(_opt(rest, "--ttl", LEASE_TTL)),
                                _opt(rest, "--desktop")))
    return _emit({"ok": False, "error": "unknown verb %s" % verb}, EXIT_USAGE)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
