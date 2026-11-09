# test-lane: fast
"""Classifier routing, isolated vendor protocols and bounded fallback."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import memory


ITEMS = ["durable fact", "new durable topic", "transient event"]
TOPICS = [("about-user.md", "About user")]
ANSWER = "1 about-user.md\n2 NEW:plans\n3 DROP\n"
VERDICTS = {1: ("FILE", "about-user.md"), 2: ("NEW", "plans"), 3: ("DROP", "")}


def response(seat, text=ANSWER):
    if seat != "codex":
        return json.dumps({"type": "result", "subtype": "success", "result": text})
    return "\n".join(json.dumps(event) for event in [
        {"type": "thread.started", "thread_id": "stub"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "stub"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {}},
    ])


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("DOGANY_CLASSIFY_SEAT", raising=False)
    monkeypatch.setenv("DOGANY_AGENT_CONF", str(tmp_path / "agent.conf"))
    monkeypatch.setattr(memory, "CLASSIFY_CMD", None)
    monkeypatch.setattr(memory, "_classify_codex_available", lambda: False)


@pytest.mark.parametrize("available,pin,expected", [
    (False, "auto", ["haiku", "opus"]),
    (True, "auto", ["codex", "haiku", "opus"]),
    (True, "haiku", ["haiku", "codex", "opus"]),
    (False, "codex", ["codex", "haiku", "opus"]),
    (True, "opus", ["opus"]),
])
def test_resolver(monkeypatch, available, pin, expected):
    monkeypatch.setattr(memory, "_classify_codex_available", lambda: available)
    monkeypatch.setenv("DOGANY_CLASSIFY_SEAT", pin)
    assert memory._resolve_classify_seats() == expected


def test_owner_conf_and_environment_precedence(monkeypatch, tmp_path):
    (tmp_path / "agent.conf").write_text("CLASSIFY_SEAT=codex\n")
    assert memory._resolve_classify_seats()[0] == "codex"
    monkeypatch.setenv("DOGANY_CLASSIFY_SEAT", "haiku")
    assert memory._resolve_classify_seats()[0] == "haiku"


@pytest.mark.parametrize("data,rc,expected", [
    ({"ok": True, "used_percent": 0}, 0, True),
    ({"ok": True, "used_percent": 98}, 0, True),
    ({"ok": True, "used_percent": 99}, 0, False),
    ({"ok": True, "used_percent": True}, 0, False),
    ({"ok": True, "used_percent": "20"}, 0, False),
    ({"ok": True, "used_percent": -1}, 0, False),
    ({"ok": False}, 2, False),
    ({"ok": True, "used_percent": 10}, 1, False),
    ([], 0, False),
])
def test_dispatch_availability_signal(monkeypatch, data, rc, expected):
    def run(cmd, **kwargs):
        assert cmd[1].endswith("routines/codex-ratelimit.py")
        assert cmd[2:] == ["--json", "--timeout", "20"]
        assert "input" not in kwargs
        return SimpleNamespace(returncode=rc, stdout=json.dumps(data))
    monkeypatch.setattr(memory.subprocess, "run", run)
    # The fixture isolates the normal resolver; exercise the real probe here.
    assert REAL_PROBE() is expected


REAL_PROBE = memory._classify_codex_available


@pytest.mark.parametrize("pin", ["haiku", "codex", "opus"])
def test_vendor_roundtrip_is_private_and_toolless(monkeypatch, pin):
    monkeypatch.setenv("DOGANY_CLASSIFY_SEAT", pin)
    monkeypatch.setenv("CLAUDECODE", "nested")
    seen = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        assert list(Path(kwargs["cwd"]).iterdir()) == []
        assert "CLAUDECODE" not in kwargs["env"]
        assert "durable fact" in kwargs["input"]
        assert "durable fact" not in " ".join(cmd)
        assert kwargs["timeout"] == 300
        if pin == "codex":
            assert "gpt-6-luna" in cmd and "model_reasoning_effort=low" in cmd
            assert cmd[cmd.index("--sandbox") + 1] == "read-only"
            for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral",
                         "features.shell_tool=false", "features.multi_agent=false"):
                assert flag in cmd
        else:
            assert cmd[cmd.index("--tools") + 1] == ""
            assert "--safe-mode" in cmd and "--no-session-persistence" in cmd
            if pin == "haiku":
                assert "claude-haiku-5-5" in cmd
                if memory.CLASSIFY_HAIKU_EFFORT:
                    assert cmd[cmd.index("--effort") + 1] == memory.CLASSIFY_HAIKU_EFFORT
                else:
                    assert "--effort" not in cmd
        return SimpleNamespace(returncode=0, stdout=response(pin))

    monkeypatch.setattr(memory.subprocess, "run", run)
    assert memory._run_classifier(ITEMS, TOPICS) == (VERDICTS, True)
    assert len(seen) == 1


@pytest.mark.parametrize("failure", ["rc", "timeout", "missing", "quota", "malformed", "partial", "tool"])
@pytest.mark.parametrize("first", ["codex", "haiku"])
def test_fallback_once_then_opus(monkeypatch, capsys, failure, first):
    monkeypatch.setenv("DOGANY_CLASSIFY_SEAT", first)
    monkeypatch.setattr(memory, "_classify_codex_available", lambda: True)
    calls = []
    expected = [first, "haiku" if first == "codex" else "codex", "opus"]

    def run(cmd, **kwargs):
        seat = "codex" if cmd[0] == "codex" else ("opus" if "opus" in cmd else "haiku")
        calls.append(seat)
        if len(calls) == 3:
            return SimpleNamespace(returncode=0, stdout=response(seat))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 300)
        if failure == "missing":
            raise FileNotFoundError("vendor missing")
        if failure == "rc":
            return SimpleNamespace(returncode=1, stdout="", stderr="private output")
        if failure == "quota":
            output = ('{"type":"turn.failed","error":{"message":"quota"}}' if seat == "codex"
                      else '{"is_error":true,"result":"quota"}')
        elif failure == "partial":
            output = response(seat, "1 DROP")
        elif failure == "tool" and seat == "codex":
            output = '{"type":"item.completed","item":{"type":"command_execution"}}'
        else:
            output = "private malformed output"
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(memory.subprocess, "run", run)
    assert memory._run_classifier(ITEMS, TOPICS) == (VERDICTS, True)
    assert calls == expected
    logs = capsys.readouterr().err
    assert "ran seat=opus" in logs
    assert "private" not in logs


def test_alternate_success_stops_fallback(monkeypatch):
    monkeypatch.setenv("DOGANY_CLASSIFY_SEAT", "codex")
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd[0])
        return SimpleNamespace(returncode=int(len(calls) == 1), stdout=response("haiku"))
    monkeypatch.setattr(memory.subprocess, "run", run)
    assert memory._run_classifier(ITEMS, TOPICS) == (VERDICTS, True)
    assert calls == ["codex", "claude"]


@pytest.mark.parametrize("deadline,expected", [(840, [300, 300, 210]), (340, [300, 10])])
def test_scheduled_fallback_reserves_checkpoint_time(monkeypatch, deadline, expected):
    clock, timeouts = [0], []
    monkeypatch.setattr(memory, "_classify_deadline", lambda: deadline)
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(memory, "_resolve_classify_seats", lambda: ["codex", "haiku", "opus"])

    def run(cmd, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(memory.subprocess, "run", run)
    assert memory._run_classifier(ITEMS, TOPICS) == ({}, False)
    assert timeouts == expected
    assert clock[0] == deadline - memory.CLASSIFY_SAVE_MARGIN


def test_scheduled_fallback_can_succeed_inside_remaining_budget(monkeypatch):
    clock, timeouts = [0], []
    monkeypatch.setattr(memory, "_classify_deadline", lambda: 500)
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(memory, "_resolve_classify_seats", lambda: ["codex", "haiku", "opus"])

    def run(cmd, **kwargs):
        timeouts.append(kwargs["timeout"])
        if len(timeouts) == 1:
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=response("haiku"))

    monkeypatch.setattr(memory.subprocess, "run", run)
    assert memory._run_classifier(ITEMS, TOPICS) == (VERDICTS, True)
    assert timeouts == [300, 170]


def test_all_fail_closed(monkeypatch):
    monkeypatch.setattr(memory.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert memory._run_classifier(ITEMS, TOPICS) == ({}, False)


def test_legacy_opus_fallback_preserves_partial_verdicts(monkeypatch):
    calls = []
    def run(cmd, **kwargs):
        seat = "opus" if "opus" in cmd else "haiku"
        calls.append(seat)
        return SimpleNamespace(returncode=0, stdout=response(seat, "1 DROP"))
    monkeypatch.setattr(memory.subprocess, "run", run)
    assert memory._run_classifier(ITEMS, TOPICS) == ({1: ("DROP", "")}, True)
    assert calls == ["haiku", "opus"]


def test_bad_pin_fails_without_vendor(monkeypatch):
    monkeypatch.setenv("DOGANY_CLASSIFY_SEAT", "cloud")
    monkeypatch.setattr(memory.subprocess, "run", lambda *a, **k: pytest.fail("vendor called"))
    assert memory._run_classifier(ITEMS, TOPICS) == ({}, False)


@pytest.mark.parametrize("command,expected", [
    ("printf '1 DROP\\n'", ({1: ("DROP", "")}, True)),
    ("exit 1", ({}, False)),
])
def test_classify_cmd_seam_never_resolves_or_falls_back(monkeypatch, command, expected):
    monkeypatch.setattr(memory, "CLASSIFY_CMD", command)
    monkeypatch.setattr(memory, "_resolve_classify_seats", lambda: pytest.fail("resolver called"))
    assert memory._run_classifier(ITEMS, TOPICS) == expected


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-rfEs"]))
