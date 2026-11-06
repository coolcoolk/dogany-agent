#!/usr/bin/env python3
# test-lane: fast
# test-tier: 0
# component: onboarding-gate-stop
# promise: Pending onboarding guidance arrives before generation.
# promise: Identity completion offers the kit before the reply closes.
# promise: Stop records completion without refusing a streamed reply.
# promise: Recording-only kit checks consume malformed closes without calling the block emitter.
# promise: Decided or unreachable kits keep their existing silent behavior.
"""Locks for the v2.6.0 rehearsal blocker (2026-10-03 16:34, fresh install,
KIT=pending): the identity-completion reply must end on the kit-select
question, not on the GENERAL menu.

Root cause: the PostToolUse continuation seat in onboarding-gate-stop.py
returned early for every non-domain instance, so a kit-pending main got no
completion-turn context and the kit offer only surfaced next session.

Contract under test:
  * rehearsal replay: main, KIT=pending, life kit reachable, the edit that
    fills the last identity field -> PostToolUse context carries the
    kit-select offer + a shape that replaces the GENERAL menu; logged
    CONTINUATION kit-select; handoff check armed (kind=kit).
  * dec-650: the same turn's Stop never blocks; completion is recorded.
    Guidance arrives at UserPromptSubmit and the completion PostToolUse.
    Successful kit-close logging remains checked separately.
  * main KIT=lifekit / KIT=none / KIT=pending with the life kit unreachable
    -> silent (existing behaviour).
  * ONBOARDING_CTX: the kit-select close outranks the menu and is not a
    quick start; Q5 (humor) carries the owner-approved line that a custom
    percent may be typed (owner 2026-10-03 21:22).

r2 (rehearsal 2026-10-03 21:21): the last field + onboarding-block removal
were written by a Bash `python3 -` script; the Edit-only PostToolUse seat
never saw the transition and the completion closed on the GENERAL menu.
  * Bash-written last field, NO PostToolUse event at all (old wiring): the
    Stop records RELEASED without blocking a GENERAL-menu reply; onboarding
    guidance is delivered before generation, not in a Stop refusal.
  * Bash-written last field WITH the PostToolUse Bash event: kit-select
    context delivered; Stop records RELEASED without refusing the reply.
  * Bash calls not naming the persona file / KIT decided -> silent.
DGN-1849: the kit question is a machine-sent screen; the completion
context names the verb's kit step instead of a two-option list (the
machine-sent close itself: tests/dgn1849_onboarding_screens_selftest.py).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(_HERE, ".."))
GATE = os.path.join(ROOT, "onboarding-gate-stop.py")
CHECK = os.path.join(ROOT, "onboarding-check.py")

MARKER = "<!-- ONBOARDING_PENDING -->"
PH = "(set at onboarding)"
_failures = []


def check(name, cond, detail=""):
    if not cond:
        _failures.append("%s %s" % (name, detail))


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def identity(humor="30%"):
    return "\n".join([
        MARKER, "# AGENT", "## Identity", "- Name: **Mori**",
        "- Emoji: \U0001F43E", "## Role", "- Primary focus: %s" % PH,
        "## Relationship", "- Call the user **Siwon**.",
        "- Tone: warm and friendly style", "- Humor: %s" % humor]) + "\n"


def mk(root, kit="pending", reachable=True):
    write(os.path.join(root, ".instance.conf"), "DOGANY_AGENT_CLASS=main\n")
    write(os.path.join(root, "config", "agent.conf"),
          "AGENT_LANG=ko\nKIT=%s\n" % kit)
    if reachable:
        write(os.path.join(root, "service", "kit_life", "bundle.conf"),
              "# fixture\n")
    if kit == "lifekit":
        write(os.path.join(root, "config", "lifekit.conf"), "LIFEKIT=pending\n")


def env_for(root):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_"))}
    for k in ("ONBOARDING_FILE", "PROJECT_ROOT", "DOGANY_SESSION",
              "AGENT_CONF_FILE", "INSTANCE_CONF_FILE", "LIFEKIT_FILE"):
        env.pop(k, None)
    env["ONBOARDING_FILE"] = root
    env["PROJECT_ROOT"] = root
    return env


def run(root, payload):
    payload = dict(payload, cwd=root)
    proc = subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30,
                          env=env_for(root))
    if proc.returncode != 0:
        _failures.append("exit %d: %s" % (proc.returncode, proc.stderr))
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def complete_identity(root):
    ident = os.path.join(root, "identity", "hot.custom.agent.md")
    write(ident, identity())
    return run(root, {"hook_event_name": "PostToolUse", "tool_name": "Edit",
                      "tool_input": {"file_path": ident,
                                     "old_string": "- Humor: " + PH,
                                     "new_string": "- Humor: 30%"}})


def log_text(root):
    try:
        with open(os.path.join(root, ".telegram_bot", "logs",
                               "onboarding-gate.log"), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


GENERAL_REPLY = ("설정 끝났어요.\n- 이름: 모리 \U0001F43E\n\n"
                 "1. 제가 뭘 해드릴 수 있는지 보기\n2. 간단한 요청 하나 해보기\n[[OPTIONS]]")
KIT_REPLY = ("설정 끝났어요.\n- 이름: 모리 \U0001F43E\n\n"
             "마지막으로 관리 대상, 킷을 정할게요. 킷은 하나만 활성화할 수 있어요.\n"
             "1. 라이프킷\n2. 킷 없이 시작\n[[OPTIONS]]")


def stop(root, text, active=False):
    return run(root, {"hook_event_name": "Stop", "stop_hook_active": active,
                      "last_assistant_message": text})


def pre_generation(root):
    # dec-650: guidance belongs before generation, never in a Stop reason.
    out = run(root, {"hook_event_name": "UserPromptSubmit", "prompt": "30%"})
    hook = (out or {}).get("hookSpecificOutput", {})
    ctx = hook.get("additionalContext", "")
    check("prompt: guidance before generation",
          hook.get("hookEventName") == "UserPromptSubmit"
          and "before generation" in ctx and "Before answering:" in ctx
          and "dogany-user-onboarding" in ctx, repr(out))
    check("prompt: PRE_GENERATION recorded", "PRE_GENERATION" in log_text(root))


def test_rehearsal_replay():
    with tempfile.TemporaryDirectory() as root:
        mk(root)
        write(ident_path(root), identity(humor=PH))
        pre_generation(root)
        out = complete_identity(root)
        check("replay: fires", out is not None)
        ctx = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        check("replay: kit-select context", "[kit selection pending]" in ctx)
        check("replay: i18n key", "'kit.offer'" in ctx)
        # DGN-1849: the kit question is a machine-sent screen (the verb's
        # kit step), no longer a model-written two-option list.
        check("replay: machine-sent kit screen",
              "onboarding-screen.py kit" in ctx and "--ack" in ctx)
        check("replay: replaces GENERAL menu", "REPLACES the GENERAL" in ctx)
        check("replay: not a quick start", "not a quick start" in ctx)
        check("replay: ask now", "ask it NOW" in ctx)
        check("replay: no domain wording", "domain onboarding" not in ctx)
        check("replay: logged", "continuation kit-select" in log_text(root))
        # dec-650: the streamed GENERAL close is not refused; record completion.
        blk = stop(root, GENERAL_REPLY)
        check("stop: general menu not blocked", not is_block(blk), repr(blk))
        check("stop: completion recorded",
              "AUTO_CLEAR" in log_text(root) and "RELEASED" in log_text(root),
              log_text(root))
        check("stop: no HANDOFF_BLOCK", "HANDOFF_BLOCK" not in log_text(root))
    with tempfile.TemporaryDirectory() as root:
        mk(root)
        complete_identity(root)
        out = stop(root, KIT_REPLY)
        check("stop: kit close passes",
              not (isinstance(out, dict) and out.get("decision") == "block"),
              repr(out))
        check("stop: HANDOFF_OK", "HANDOFF_OK" in log_text(root))


def test_kit_decided_or_unreachable_silent():
    for kit, reachable, name in (("lifekit", True, "lifekit"),
                                 ("none", True, "none"),
                                 ("pending", False, "unreachable")):
        with tempfile.TemporaryDirectory() as root:
            mk(root, kit=kit, reachable=reachable)
            check("%s: silent" % name, complete_identity(root) is None)
            check("%s: no handoff armed" % name, not os.path.exists(
                os.path.join(root, ".telegram_bot", "logs",
                             ".onboarding-handoff-due")))


def test_onboarding_ctx_rules():
    spec = importlib.util.spec_from_file_location("oc_v260", CHECK)
    oc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oc)
    en, ko = oc.ONBOARDING_CTX["en"], oc.ONBOARDING_CTX["ko"]
    check("ctx en: kit-select close", "KIT-SELECT CLOSE" in en
          and "INSTEAD of this menu" in en and "not a quick start" in en)
    check("ctx ko: kit-select close", "킷 선택 마무리" in ko
          and "빠른 시작이 아니" in ko)
    check("ctx en: kit-neutral rule kept",
          "never a life-assistant or any other domain quick start" in en)
    check("ctx ko: Q5 owner line",
          "'유머 수치를 몇 %로 설정할까요? 원하는 수치를 직접 적어 주셔도 돼요.'" in ko)
    check("ctx ko: Q5 old phrasing gone",
          "다른 수치는 직접 입력하셔도" not in ko and "안내 문장은 붙이지 않습니다" not in ko)
    check("ctx en: Q5 custom-percent line",
          "'What percent should I set the humor to? You can also type the value "
          "you want.'" in en and "never say that other values may be typed" not in en)


# --- r2: identity completed by a Bash script ------------------------------

def ident_path(root):
    return os.path.join(root, "identity", "hot.custom.agent.md")


def start_bash_cycle(root, kit="pending"):
    """Fields Q1..Q4 set, Humor still the placeholder, marker present; the
    owner's turn opens (UserPromptSubmit arms the gate), then a Bash script
    writes Humor and drops the marker line in one go."""
    mk(root, kit=kit)
    write(ident_path(root), identity(humor=PH))
    pre_generation(root)
    write(ident_path(root), identity().replace(MARKER + "\n", ""))


BASH_CMD = ("python3 - <<'EOF'\nimport re\np='identity/hot.custom.agent.md'\n"
            "s=open(p).read()\n...\nEOF")


def is_block(out):
    return isinstance(out, dict) and out.get("decision") == "block"


def test_recording_checks_consume_without_refusal():
    spec = importlib.util.spec_from_file_location("kit_close_gate", GATE)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    # Narrow fixtures: completed identity, kit availability/config and the
    # one sentinel each seat owns. No transcript or service tree is needed.
    for seat in ("handoff", "release"):
        with tempfile.TemporaryDirectory() as root:
            mk(root)
            write(ident_path(root), identity().replace(MARKER + "\n", ""))
            data = {"cwd": root, "hook_event_name": "Stop",
                    "last_assistant_message": GENERAL_REPLY}
            with patch.dict(os.environ, env_for(root), clear=True), patch.object(
                    gate, "emit_block", side_effect=AssertionError(
                        "dec-650: recording-only checks must never emit a block")):
                if seat == "handoff":
                    gate.arm_handoff_check(root, kind="kit")
                    result = gate.handoff_check(data, root, record_only=True)
                    check("record/handoff: consumed", not os.path.exists(
                        gate._handoff_due_path(root)))
                else:
                    result = gate.completion_kit_check(
                        data, root, ident_path(root), record_only=True)
                    check("record/release: consumed", os.path.exists(os.path.join(
                        gate._resolve_log_dir(root), gate._CONTINUATION_SENT_FILENAME)))
                check("record/%s: no refusal" % seat, result is False)
                check("record/%s: no HANDOFF_BLOCK" % seat,
                      "HANDOFF_BLOCK" not in log_text(root))


def test_bash_completion_stop_only():
    with tempfile.TemporaryDirectory() as root:
        start_bash_cycle(root)
        # dec-650: Stop records release; guidance was asserted before generation.
        blk = stop(root, GENERAL_REPLY)
        check("bash/stop: general menu not blocked", not is_block(blk), repr(blk))
        log = log_text(root)
        check("bash/stop: RELEASED without HANDOFF_BLOCK",
              "RELEASED" in log and "marker cleared" in log
              and "HANDOFF_BLOCK" not in log, log)
        again = stop(root, GENERAL_REPLY, active=True)
        check("bash/stop: blocks once", not is_block(again), repr(again))
        later = stop(root, GENERAL_REPLY)
        check("bash/stop: later turn silent", not is_block(later), repr(later))
    with tempfile.TemporaryDirectory() as root:
        start_bash_cycle(root)
        out = stop(root, KIT_REPLY)
        check("bash/stop: kit close passes", not is_block(out), repr(out))
        check("bash/stop: HANDOFF_OK", "HANDOFF_OK" in log_text(root))
    with tempfile.TemporaryDirectory() as root:
        start_bash_cycle(root)
        # dec-650: a missing options marker cannot trigger a Stop refusal.
        out = stop(root, KIT_REPLY.replace("\n[[OPTIONS]]", ""))
        check("bash/stop: no marker not blocked", not is_block(out), repr(out))
        check("bash/stop: no marker release recorded",
              "RELEASED" in log_text(root) and "HANDOFF_BLOCK" not in log_text(root),
              log_text(root))


def test_bash_completion_posttooluse():
    with tempfile.TemporaryDirectory() as root:
        start_bash_cycle(root)
        out = run(root, {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                         "tool_input": {"command": BASH_CMD}})
        ctx = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        check("bash/ptu: kit-select context", "[kit selection pending]" in ctx
              and "REPLACES the GENERAL" in ctx, repr(out))
        check("bash/ptu: once", run(root, {
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": BASH_CMD}}) is None)
        # dec-650: PostToolUse supplies the kit offer; Stop only records release.
        blk = stop(root, GENERAL_REPLY)
        check("bash/ptu: general menu not blocked", not is_block(blk), repr(blk))
        check("bash/ptu: single block", not is_block(
            stop(root, GENERAL_REPLY, active=True)))
        check("bash/ptu: one RELEASED, no HANDOFF_BLOCK",
              log_text(root).count("RELEASED") == 1
              and "HANDOFF_BLOCK" not in log_text(root), log_text(root))
    with tempfile.TemporaryDirectory() as root:
        start_bash_cycle(root)
        out = run(root, {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                         "tool_input": {"command": "ls -la && git status"}})
        check("bash/ptu: unrelated command silent", out is None, repr(out))


def test_bash_completion_kit_decided_silent():
    for kit in ("lifekit", "none"):
        with tempfile.TemporaryDirectory() as root:
            start_bash_cycle(root, kit=kit)
            out = run(root, {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                             "tool_input": {"command": BASH_CMD}})
            check("bash %s: ptu silent" % kit, out is None, repr(out))
            out = stop(root, GENERAL_REPLY)
            check("bash %s: stop passes" % kit, not is_block(out), repr(out))
            check("bash %s: released, no handoff" % kit,
                  "RELEASED" in log_text(root)
                  and "HANDOFF" not in log_text(root), log_text(root))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if _failures:
        print("FAIL (%d)" % len(_failures))
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("OK test-v260-kit-offer-at-completion")
