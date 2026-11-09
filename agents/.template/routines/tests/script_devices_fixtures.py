"""Shared live-session fixture for script-device integration tests.

This is the surviving fixture seam from the retired D3 Bash-gate suite, kept
separate from product assertions so consumers do not load deleted tests.
"""
import datetime
import importlib.util
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

ROUTINES = Path(__file__).resolve().parent.parent
TRASH = str(ROUTINES / "lib" / "trash.sh")
_TMP_BASE = os.environ.get("V27_D34_TMP") or os.path.expanduser("~")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE = _load("bash_safety_gate_device_fixture", ROUTINES / "bash-safety-gate.py")


class Session(object):
    """A fixture instance root + one session transcript + the owner-turn
    ledger the UserPromptSubmit hook (`why-net ups`) would have written."""

    def __init__(self, files=None, canonical=False):
        # not under /tmp: /tmp is a scratch root, where rm is never gated
        self.tmp = tempfile.mkdtemp(prefix=".v27d34-", dir=_TMP_BASE)
        self.root = os.path.join(self.tmp, "inst")
        self.scratch = tempfile.mkdtemp(prefix="v27d34-scratch-", dir=os.environ.get("TMPDIR", "/tmp"))
        if canonical:
            # F20: the canonical checkout carries neither .telegram_bot/ nor
            # identity/; its data dir lives elsewhere
            self.data = os.path.join(self.tmp, "data")
            os.makedirs(self.data)
            os.makedirs(os.path.join(self.root, "agents", ".template"))
            dirs = ("files/tmp", "playbooks", "config")
        else:
            self.data = os.path.join(self.root, ".telegram_bot")
            os.makedirs(self.data)
            dirs = ("files/tmp", "identity", "playbooks", "config")
        for d in dirs:
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        for rel, text in (files or {}).items():
            p = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.sid = "sess-" + uuid.uuid4().hex[:8]
        self.transcript = os.path.join(self.tmp, self.sid + ".jsonl")
        open(self.transcript, "w").close()
        self.ledger = os.path.join(self.data, "state", "why-net",
                                   "turn-ledger.jsonl")
        self.last = None
        self.last_tool = None
        self.uy = _load("user_yes_session", ROUTINES / "lib" / "user_yes.py")

    def sub(self, s, cmd=None, key=None):
        s = s.replace("{root}", self.root).replace("{trash}", TRASH) \
            .replace("{tmp}", self.tmp).replace("{scratch}", self.scratch)
        # R2-1: {q} / {qko} = the gate's question line for this step's
        # command, {q:CMD} for another command, {qpath:REL} for a D4 path
        # (key from the step)
        for tag, kind, lang in (("{q:", "cmd", 0), ("{qko:", "cmd", 1),
                                ("{qpath:", "path", 0)):
            while tag in s:
                i = s.index(tag)
                j = s.index("}", i)
                lab = self.sub(s[i + len(tag):j])
                k = key if kind == "path" else self.uy.action_key("cmd", lab)
                s = s[:i] + self.uy.question_lines(kind, lab, k)[lang] + \
                    s[j + 1:]
        if cmd is not None:
            k = key or self.uy.action_key("cmd", cmd)
            s = s.replace("{q}", self.uy.question_lines("cmd", cmd, k)[0]) \
                .replace("{qko}", self.uy.question_lines("cmd", cmd, k)[1])
        while "{key:" in s:
            i = s.index("{key:")
            j = s.index("}", i)
            s = s[:i] + self.uy.action_key("cmd", self.sub(s[i + 5:j])) + \
                s[j + 1:]
        if key is not None:
            s = s.replace("{key}", key)
        elif cmd is not None:
            s = s.replace("{key}", self.uy.action_key("cmd", cmd))
        return s

    def _ledger(self, text, ts, source, extra=None):
        os.makedirs(os.path.dirname(self.ledger), exist_ok=True)
        wot = self.uy._wot()
        row = {"id": "otl-%s" % uuid.uuid4().hex[:6],
               "at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "session_id": self.sid, "sha": wot.turn_sha(text),
               "len": len(text), "request_id": uuid.uuid4().hex,
               "source": source, "transcript_path": self.transcript}
        row.update(extra or {})
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def add(self, turns, cmd=None, key=None):
        now = datetime.datetime.now(datetime.timezone.utc)
        for t in turns:
            u = uuid.uuid4().hex
            when = now - datetime.timedelta(seconds=t.get("ago", 0))
            ts = when.isoformat()
            base = {"uuid": u, "parentUuid": self.last, "timestamp": ts,
                    "sessionId": self.sid}
            if "queued_owner" in t:
                text = self.sub(t["queued_owner"], cmd, key)
                e = dict(base, type="attachment", userType=t.get("userType", "external"),
                         attachment={"type": "queued_command", "prompt": text,
                                     "commandMode": t.get("commandMode", "prompt")})
                if not t.get("unledgered"):
                    self._ledger(text, when, t.get("source", "message"))
            elif "owner" in t or "injected" in t:
                text = self.sub(t.get("owner", t.get("injected")), cmd, key)
                e = dict(base, type="user", userType="external",
                         message={"role": "user", "content": text})
                if "owner" in t and not t.get("unledgered"):
                    self._ledger(text, when, t.get("source", "message"),
                                 {k: t[k] for k in ("chat_id", "message_id")
                                  if k in t})
            elif "say" in t:
                e = dict(base, type="assistant", message={
                    "role": "assistant", "content": [
                        {"type": "text",
                         "text": self.sub(t["say"], cmd, key)}]})
            elif "bubble" in t:
                # R2-2: the bridge's record of the bubble that carried a
                # keyboard (sdk_bridge.publish_options_bubble), written now
                path = os.path.join(self.data, "options-bubbles.json")
                try:
                    with open(path, encoding="utf-8") as fh:
                        rows = json.load(fh)
                except (OSError, ValueError):
                    rows = []
                rows.append(dict(t["bubble"], ts=time.time() +
                                 t["bubble"].get("dt", 0)))
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(rows, fh, ensure_ascii=False)
                continue
            elif "tool_result" in t:
                block = {"type": "tool_result",
                         "tool_use_id": self.last_tool or "toolu_x",
                         "content": t["tool_result"]}
                if t.get("error"):
                    block["is_error"] = True
                e = dict(base, type="user", userType="external",
                         toolUseResult={"stdout": t["tool_result"]},
                         message={"role": "user", "content": [block]})
            else:
                tool = t["tool"]
                self.last_tool = "toolu_" + u[:8]
                e = dict(base, type="assistant", message={
                    "role": "assistant", "content": [{
                        "type": "tool_use", "id": self.last_tool,
                        "name": tool["name"],
                        "input": json.loads(self.sub(json.dumps(
                            tool["input"]), cmd, key))}]})
            with open(self.transcript, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            self.last = u

    def payload(self, tool_name, tool_input, agent_type=None, cwd=None,
                permission_mode=None, tool_use_id=None):
        d = {"hook_event_name": "PreToolUse", "tool_name": tool_name,
             "tool_input": tool_input, "session_id": self.sid,
             "transcript_path": self.transcript, "cwd": cwd or self.root}
        if tool_use_id:
            d["tool_use_id"] = tool_use_id
        if agent_type:
            d["agent_id"] = "agent-" + agent_type
            d["agent_type"] = agent_type
        if permission_mode:
            d["permission_mode"] = permission_mode
        return d

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.scratch, ignore_errors=True)

