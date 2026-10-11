"""DGN-1209: outbound machine-line gate -- contract, choke-point wiring, reader.

The leak: set-table printed `DAY_SOURCE ...` / `RAMP_DECISION ...` to the same
stdout as the fenced table; two zero-model-turn rails (idrill followup_cmd,
fastpath body) posted it verbatim and the owner read internal diagnostics
on his phone. The fix is ONE pure gate (bridge/machine_gate.py) called from
the two render entry points every rail goes through, with the rail DECLARED
at the call site.

Contract under test (ticket "확정 방향" + grill LOCK-WITH-FIX):

    registered token + pure machine line   -> DROP  + log
    registered token + owner sentence      -> STRIP prefix, sentence passes
    unregistered + machine SHAPE           -> PASS  + alert (dedup (rail, token, day))
    everything else                        -> PASS

Sections:
  A  pure gate (no I/O)          -- the leaked strings are blocked; normal
                                    Korean lines with uppercase acronyms are not
  B  apply wrapper + alert sink  -- rail decides alert vs log; unknown says so
  C  dedup ledger                -- (rail, token, day) durable markers
  D  wiring                      -- every rail declares into the gate (R1..R4)
  E  emitter helper              -- sigil wire format == bridge constant
  F  copy / marker registration  -- i18n keys present, sigil is a known marker
  G  bot-side reader             -- query-time ledger regardless of steward;
                                    no chat delivery or session injection

The COUNTER-EXAMPLE test (set-table:2771 direct print bypass) pins the
detector as a permanent component: delete the heuristic and it fails.

Pure-function tests use no fixtures/env beyond conftest's pins (grill F:
"반례 테스트 무의존화"). No live Telegram, token, or network.
"""

import asyncio
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge import config as config_mod
from bridge import machine_gate as mg
from bridge.formatting import (
    RECOGNIZED_COLON_MARKERS,
    sanitize_message_for_telegram,
)
from bridge.i18n import en, ko
from bridge.machine_gate import (
    MACHINE_SIGNAL_MARKER,
    RAIL_CONSUMER,
    RAIL_MODEL,
    RAIL_UNKNOWN,
    MachineLineAlertLedger,
    apply_machine_line_gate,
    format_alert_text,
    gate_machine_lines,
)

from bridge.tests._hostroot import requires_host

BRIDGE_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = BRIDGE_DIR.parent
HELPER_PATH = TEMPLATE_DIR / "routines" / "lib" / "machine_signal.py"
# Both halves live in the HOST instance root, not in bridge/: the public
# distribution ships bridge/ alone, so these tests skip there rather than
# reporting a failure whose only cause is "no instance root" (_hostroot).
requires_push = requires_host("routines/push.sh")
requires_helper = requires_host("routines/lib/machine_signal.py")
PUSH_SH = TEMPLATE_DIR / "routines" / "push.sh"

# The two lines the owner actually received (ticket §1), verbatim shape.
LEAK_DAY_SOURCE = "DAY_SOURCE exercise=smith_seated_calf_raise day=뒷B source=session_type"
LEAK_RAMP = "RAMP_DECISION exercise=smith_seated_calf_raise decision=ramp reason=machine_ladder rungs=2 target_weight=45.0"
TABLE = "```\n| SET | WT | REPS |\n| 1 | 45.0 | 8 |\n```"
NORMAL_KO = "API 문서 갱신했습니다"


@pytest.fixture(autouse=True)
def _reset_sink():
    yield
    mg.set_machine_line_alert_sink(None)


# ===========================================================================
# A. pure gate
# ===========================================================================

def test_leaked_day_source_and_ramp_decision_are_dropped_table_kept():
    text = f"{TABLE}\n{LEAK_DAY_SOURCE}\n{LEAK_RAMP}\n"
    r = gate_machine_lines(text, RAIL_CONSUMER)
    assert "DAY_SOURCE" not in r.text
    assert "RAMP_DECISION" not in r.text
    assert r.text == TABLE + "\n"
    assert [t for t, _ in r.dropped] == ["DAY_SOURCE", "RAMP_DECISION"]
    assert r.unregistered == ()


def test_every_seed_token_kv_form_is_dropped():
    for tok, action in mg.SEED_MACHINE_TOKENS.items():
        r = gate_machine_lines(f"{tok} a=1 b=2\n", RAIL_CONSUMER)
        assert r.text == "", tok
        assert r.dropped[0][0] == tok


def test_load_decision_prefix_stripped_sentence_kept():
    r = gate_machine_lines("LOAD_DECISION: 8회 성공이라 2.5kg 올렸습니다\n", RAIL_CONSUMER)
    assert r.text == "8회 성공이라 2.5kg 올렸습니다\n"
    assert r.stripped == (("LOAD_DECISION", "8회 성공이라 2.5kg 올렸습니다"),)
    assert r.dropped == ()


def test_strip_token_with_empty_payload_is_dropped_not_blank():
    r = gate_machine_lines("LOAD_DECISION:\nnext\n", RAIL_CONSUMER)
    assert r.text == "next\n"
    assert r.dropped == (("LOAD_DECISION", "LOAD_DECISION:"),)


def test_normal_korean_line_with_uppercase_acronym_passes_unchanged():
    # 리뷰 반론 1: "API 문서 갱신했습니다" must NOT be blocked by the shape rule.
    text = f"{NORMAL_KO}\nPR 올렸습니다.\n"
    r = gate_machine_lines(text, RAIL_CONSUMER)
    assert r.text == text
    assert r.dropped == () and r.stripped == ()
    # ...but it IS reported (detection only). "PR" is 2 chars -> not a hit.
    assert r.unregistered == ("API",)


def test_set_table_2771_direct_print_bypass_is_detected_not_dropped():
    """COUNTER-EXAMPLE (permanent): an emitter that bypasses the helper leaks
    ONCE, loudly. The detector must react to an unregistered machine-shaped
    line; removing the heuristic 'because the helper guarantees registration'
    re-opens the class. set-table:2771 is the standing instance."""
    line = "PREP_PLANNED_V2 slot=1 drills=3"
    r = gate_machine_lines(line + "\n", RAIL_CONSUMER)
    assert r.text == line + "\n"           # never censored by shape
    assert r.unregistered == ("PREP_PLANNED_V2",)   # but never silent


def test_longer_word_sharing_registered_prefix_is_not_the_token():
    r = gate_machine_lines("RAMP_DECISIONS are listed below\n", RAIL_CONSUMER)
    assert r.dropped == ()
    assert r.text == "RAMP_DECISIONS are listed below\n"
    assert r.unregistered == ("RAMP_DECISIONS",)


def test_token_inside_prose_is_not_matched():
    text = "오늘 DAY_SOURCE 값은 세션 타입입니다\n"
    r = gate_machine_lines(text, RAIL_CONSUMER)
    assert r.text == text and r.dropped == () and r.unregistered == ()


def test_sigil_kv_form_dropped_and_colon_form_stripped():
    text = (
        f"{MACHINE_SIGNAL_MARKER}RAMP_DECISION exercise=squat decision=ramp\n"
        f"{MACHINE_SIGNAL_MARKER}LOAD_DECISION: 8회 성공이라 올렸습니다\n"
        f"{MACHINE_SIGNAL_MARKER}NEW_TOKEN_NOBODY_REGISTERED k=v\n"
        "본문 문장\n"
    )
    r = gate_machine_lines(text, RAIL_CONSUMER)
    assert r.text == "8회 성공이라 올렸습니다\n본문 문장\n"
    assert [t for t, _ in r.dropped] == ["RAMP_DECISION", "NEW_TOKEN_NOBODY_REGISTERED"]
    assert r.stripped == (("LOAD_DECISION", "8회 성공이라 올렸습니다"),)
    # A sigil line is REGISTERED by construction -> never an unregistered hit.
    assert r.unregistered == ()


def test_sigil_malformed_payload_is_still_machine_and_dropped():
    r = gate_machine_lines(f"{MACHINE_SIGNAL_MARKER}lower-case junk\n", RAIL_CONSUMER)
    assert r.text == ""
    assert len(r.dropped) == 1


def test_inline_triple_backtick_does_not_hide_machine_line():
    # grill 깨진 전제 3: text.find("```") opened a fence on inline backticks
    # and the machine line behind it went out as <pre>. Line-anchored now.
    text = f"설명 ``` 인라인 백틱\n{LEAK_DAY_SOURCE}\n"
    r = gate_machine_lines(text, RAIL_CONSUMER)
    assert "DAY_SOURCE" not in r.text
    assert r.dropped[0][0] == "DAY_SOURCE"


def test_lines_inside_a_closed_fence_are_left_alone():
    text = "```\nDAY_SOURCE inside=code\n```\n"
    r = gate_machine_lines(text, RAIL_CONSUMER)
    assert r.text == text and r.dropped == ()


def test_unclosed_fence_is_treated_as_prose():
    text = f"```\n{LEAK_RAMP}\n"
    r = gate_machine_lines(text, RAIL_CONSUMER)
    assert "RAMP_DECISION" not in r.text
    assert r.text == "```\n"


def test_crlf_lines_are_matched_and_endings_preserved():
    text = f"{LEAK_DAY_SOURCE}\r\nLOAD_DECISION: 문장\r\n본문\r\n"
    r = gate_machine_lines(text, RAIL_CONSUMER)
    assert r.text == "문장\r\n본문\r\n"


def test_indented_machine_line_is_still_matched():
    r = gate_machine_lines(f"   {LEAK_RAMP}\n", RAIL_CONSUMER)
    assert r.text == ""


def test_machine_only_body_becomes_empty():
    r = gate_machine_lines(f"{LEAK_DAY_SOURCE}\n{LEAK_RAMP}", RAIL_CONSUMER)
    assert r.text == ""
    assert apply_machine_line_gate(f"{LEAK_DAY_SOURCE}\n{LEAK_RAMP}", RAIL_CONSUMER) == ""


def test_empty_and_none_like_inputs_pass_through():
    assert gate_machine_lines("", RAIL_CONSUMER).text == ""
    assert apply_machine_line_gate("", RAIL_CONSUMER) == ""


def test_gate_disabled_passes_everything(monkeypatch):
    monkeypatch.setattr(config_mod, "BRIDGE_MACHINE_LINE_GATE", False)
    text = f"{LEAK_DAY_SOURCE}\n"
    r = gate_machine_lines(text, RAIL_CONSUMER)
    assert r.text == text and r.dropped == () and r.unregistered == ()


def test_env_token_registry_extends_seed(monkeypatch):
    monkeypatch.setattr(config_mod, "BRIDGE_MACHINE_TOKENS", "FOO_SIG, BAR_SAY:strip, bad token, BAZ:weird")
    reg = mg.registered_tokens()
    assert reg["FOO_SIG"] == mg.ACTION_DROP
    assert reg["BAR_SAY"] == mg.ACTION_STRIP
    assert "bad token" not in reg and "BAZ" not in reg
    assert reg["DAY_SOURCE"] == mg.ACTION_DROP           # seed still there
    r = gate_machine_lines("FOO_SIG x=1\nBAR_SAY: 형님 문장\n", RAIL_CONSUMER)
    assert r.text == "형님 문장\n"


def test_gate_applies_identically_on_model_rail():
    # DROP/STRIP is rail-independent (both rails reach the owner); only the
    # ALERT differs. Registered tokens drop on the model rail too.
    r = gate_machine_lines(f"{LEAK_RAMP}\n답변\n", RAIL_MODEL)
    assert r.text == "답변\n"


# ===========================================================================
# B. apply wrapper + alert sink (rail decides alert vs log)
# ===========================================================================

def _capture_sink():
    calls = []
    mg.set_machine_line_alert_sink(lambda rail, tokens, sample: calls.append((rail, tokens, sample)))
    return calls


def test_consumer_rail_unregistered_shape_alerts_with_sample():
    calls = _capture_sink()
    out = apply_machine_line_gate(f"{NORMAL_KO}\nFOO_BAR k=v\n", RAIL_CONSUMER)
    assert out == f"{NORMAL_KO}\nFOO_BAR k=v\n"      # passed, not censored
    assert len(calls) == 1
    rail, tokens, sample = calls[0]
    assert rail == RAIL_CONSUMER
    assert tokens == ("API", "FOO_BAR")
    assert sample == NORMAL_KO


def test_model_rail_unregistered_shape_logs_only_no_alert():
    calls = _capture_sink()
    out = apply_machine_line_gate("RIR 2로 맞추세요\nNO_PUSH today\n", RAIL_MODEL)
    assert out == "RIR 2로 맞추세요\nNO_PUSH today\n"
    assert calls == []


def test_unknown_rail_alerts_and_copy_says_undeclared():
    calls = _capture_sink()
    apply_machine_line_gate("FOO_BAR k=v\n", RAIL_UNKNOWN)
    assert calls and calls[0][0] == RAIL_UNKNOWN
    text = format_alert_text(RAIL_UNKNOWN, ["FOO_BAR"])
    assert "FOO_BAR" in text and RAIL_UNKNOWN in text
    # The undeclared suffix is appended ONLY for rail=unknown.
    from bridge.formatting import _i18n
    suffix = _i18n("machine_line_alert_undeclared", "")
    assert suffix and text.endswith(suffix)
    assert not format_alert_text(RAIL_CONSUMER, ["FOO_BAR"]).endswith(suffix)


def test_bogus_rail_string_is_treated_as_unknown():
    calls = _capture_sink()
    apply_machine_line_gate("FOO_BAR k=v\n", "totally-made-up")
    assert calls and calls[0][0] == RAIL_UNKNOWN


def test_registered_drop_never_reaches_the_sink():
    calls = _capture_sink()
    apply_machine_line_gate(f"{LEAK_DAY_SOURCE}\n", RAIL_CONSUMER)
    assert calls == []


def test_sink_exception_never_breaks_the_send():
    def boom(*_a):
        raise RuntimeError("sink down")
    mg.set_machine_line_alert_sink(boom)
    assert apply_machine_line_gate("FOO_BAR k=v\n", RAIL_CONSUMER) == "FOO_BAR k=v\n"


def test_default_sink_is_log_only_and_says_no_reader(caplog):
    mg.set_machine_line_alert_sink(None)
    with caplog.at_level("WARNING", logger="bridge.machine_gate"):
        apply_machine_line_gate("FOO_BAR k=v\n", RAIL_CONSUMER)
    assert any("NO ALERT SINK REGISTERED" in rec.message for rec in caplog.records)


def test_drop_is_logged_never_silent(caplog):
    with caplog.at_level("WARNING", logger="bridge.machine_gate"):
        apply_machine_line_gate(f"{LEAK_RAMP}\n", RAIL_CONSUMER)
    msgs = [r.message for r in caplog.records]
    assert any("DROP" in m and "RAMP_DECISION" in m for m in msgs)


# ===========================================================================
# C. dedup ledger -- (rail, token, day), durable
# ===========================================================================

def test_ledger_dedups_per_rail_token_day(tmp_path):
    led = MachineLineAlertLedger(tmp_path / "ml")
    assert led.unseen(RAIL_CONSUMER, ["A_B", "A_B", "C_D"], "20260901") == ["A_B", "C_D"]
    led.mark(RAIL_CONSUMER, ["A_B"], "20260901")
    assert (tmp_path / "ml" / "consumer.A_B.20260901").exists()
    assert led.unseen(RAIL_CONSUMER, ["A_B", "C_D"], "20260901") == ["C_D"]
    # Same token on a NEW rail is the event the detector exists for -> unseen.
    assert led.unseen(RAIL_UNKNOWN, ["A_B"], "20260901") == ["A_B"]
    # New day -> unseen again.
    assert led.unseen(RAIL_CONSUMER, ["A_B"], "20260902") == ["A_B"]


def test_ledger_purges_markers_older_than_ttl(tmp_path):
    led = MachineLineAlertLedger(tmp_path / "ml")
    led.mark(RAIL_CONSUMER, ["OLD_ONE"], "20260101")
    led.mark(RAIL_CONSUMER, ["NEW_ONE"], "20260901")
    names = sorted(p.name for p in (tmp_path / "ml").iterdir())
    assert names == ["consumer.NEW_ONE.20260901"]


def test_ledger_unwritable_dir_is_soft(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    led = MachineLineAlertLedger(blocker / "sub")   # parent is a file -> mkdir fails
    led.mark(RAIL_CONSUMER, ["A_B"])                # must not raise
    assert led.unseen(RAIL_CONSUMER, ["A_B"]) == ["A_B"]


# ===========================================================================
# D. wiring -- every rail declares into the gate
# ===========================================================================

def test_sanitize_entry_point_drops_leak_and_keeps_table():
    calls = _capture_sink()
    html = sanitize_message_for_telegram(f"{TABLE}\n{LEAK_DAY_SOURCE}\n{LEAK_RAMP}\n", rail=RAIL_CONSUMER)
    assert "DAY_SOURCE" not in html and "RAMP_DECISION" not in html
    assert "<pre>" in html and "45.0" in html
    assert calls == []


def test_sanitize_entry_point_machine_only_returns_empty():
    assert sanitize_message_for_telegram(f"{LEAK_DAY_SOURCE}\n{LEAK_RAMP}", rail=RAIL_CONSUMER) == ""


def test_sanitize_entry_point_default_rail_is_unknown_and_alerts():
    calls = _capture_sink()
    sanitize_message_for_telegram("FOO_BAR k=v\n")
    assert calls and calls[0][0] == RAIL_UNKNOWN


def test_render_prose_html_segments_drops_leak_keeps_table():
    from bridge.bot import TelegramBot
    parts = TelegramBot._render_prose_html_segments(
        f"{TABLE}\n{LEAK_DAY_SOURCE}\n{LEAK_RAMP}\n{NORMAL_KO}\n", rail=RAIL_CONSUMER
    )
    joined = "\n".join(parts)
    assert "DAY_SOURCE" not in joined and "RAMP_DECISION" not in joined
    assert "<pre>" in joined
    assert NORMAL_KO in joined


def test_render_prose_html_segments_machine_only_renders_nothing():
    from bridge.bot import TelegramBot
    assert TelegramBot._render_prose_html_segments(f"{LEAK_RAMP}\n", rail=RAIL_CONSUMER) == []


# ESTATE:BEGIN R1 rail suite -- exercises machine_gate through the bot.py idrill followup engine, which regions delete; the gate itself stays covered by the rail-unit and R2 fastpath cases
# --- R1: idrill followup_cmd stdout (end-to-end through the callback fire) ---

@pytest.fixture
def idrill_env(tmp_path):
    import bridge.bot as bot_mod
    from bridge.bot import TelegramBot
    from bridge.tests import test_dgn939_followup_cmd as f
    cb = tmp_path / "files" / "program" / ".idrill-arm"
    cb.mkdir(parents=True)
    with patch.object(bot_mod, "PROJECT_ROOT", tmp_path):
        yield TelegramBot.__new__(TelegramBot), cb, f


def test_r1_idrill_followup_stdout_is_gated_before_posting(idrill_env, monkeypatch):
    bot, arm_dir, f = idrill_env
    calls = _capture_sink()
    f.write_arm(arm_dir, "abcd1234", extra={"followup_cmd": [f.FOLLOWUP_BIN, "sess-abc123", "{1}", "{2}"]})
    q = f.FakeQuery("idrill:abcd1234:2:2")
    subprocess_calls = []
    f.patch_run_by_bin(monkeypatch, {
        f.CMD_BIN: f.make_proc(0, "recorded"),
        f.FOLLOWUP_BIN: f.make_proc(0, f"{TABLE}\n{LEAK_DAY_SOURCE}\n{LEAK_RAMP}\nLOAD_DECISION: 8회 성공이라 올렸습니다\n"),
    }, subprocess_calls)

    f.run(bot._handle_idrill_callback(q, q.data))

    posted = "\n".join(text for text, _ in q.message.posts)
    assert posted, "followup must still post the table"
    assert "DAY_SOURCE" not in posted and "RAMP_DECISION" not in posted
    assert "LOAD_DECISION" not in posted
    assert "8회 성공이라 올렸습니다" in posted
    assert "<pre>" in posted
    assert calls == []          # everything registered -> no alert


def test_r1_idrill_followup_machine_only_stdout_posts_nothing(idrill_env, monkeypatch):
    bot, arm_dir, f = idrill_env
    f.write_arm(arm_dir, "abcd1234", extra={"followup_cmd": [f.FOLLOWUP_BIN, "sess-abc123", "{1}", "{2}"]})
    q = f.FakeQuery("idrill:abcd1234:2:2")
    f.patch_run_by_bin(monkeypatch, {
        f.CMD_BIN: f.make_proc(0, "recorded"),
        f.FOLLOWUP_BIN: f.make_proc(0, f"{LEAK_DAY_SOURCE}\n{LEAK_RAMP}\n"),
    }, [])
    f.run(bot._handle_idrill_callback(q, q.data))
    assert q.message.posts == []
    assert len(q.text_edits) == 1   # confirmation edit still happened


def test_r1_idrill_followup_unregistered_shape_alerts_on_consumer_rail(idrill_env, monkeypatch):
    bot, arm_dir, f = idrill_env
    calls = _capture_sink()
    f.write_arm(arm_dir, "abcd1234", extra={"followup_cmd": [f.FOLLOWUP_BIN, "sess-abc123", "{1}", "{2}"]})
    q = f.FakeQuery("idrill:abcd1234:2:2")
    f.patch_run_by_bin(monkeypatch, {
        f.CMD_BIN: f.make_proc(0, "recorded"),
        f.FOLLOWUP_BIN: f.make_proc(0, "SET 1 done\nBRAND_NEW_SIGNAL k=v\n"),
    }, [])
    f.run(bot._handle_idrill_callback(q, q.data))
    posted = "\n".join(text for text, _ in q.message.posts)
    assert "BRAND_NEW_SIGNAL" in posted           # passed (no censorship by shape)
    assert calls and calls[0][0] == RAIL_CONSUMER
    assert "BRAND_NEW_SIGNAL" in calls[0][1]
# ESTATE:END


# --- R2: fastpath body ---

def test_r2_fastpath_push_declares_consumer_rail():
    from bridge.bot import TelegramBot
    bot = TelegramBot.__new__(TelegramBot)
    bot._send_smart = AsyncMock()
    asyncio.run(bot._fastpath_push_guaranteed(42, f"{TABLE}\n{LEAK_RAMP}"))
    assert bot._send_smart.await_count == 1
    assert bot._send_smart.await_args.kwargs.get("rail") == RAIL_CONSUMER


def test_r2_send_smart_threads_rail_to_render(monkeypatch):
    from bridge.bot import TelegramBot
    bot = TelegramBot.__new__(TelegramBot)
    bot.application = MagicMock()
    bot.application.bot.send_message = AsyncMock()
    bot._send_content_artifacts = AsyncMock()
    seen = {}
    real = TelegramBot._render_prose_html_segments

    def spy(content, rail=RAIL_UNKNOWN):
        seen["rail"] = rail
        seen["content"] = content
        return real(content, rail=rail)

    monkeypatch.setattr(TelegramBot, "_render_prose_html_segments", staticmethod(spy))
    asyncio.run(bot._send_smart(42, f"{TABLE}\n{LEAK_RAMP}", rail=RAIL_CONSUMER))
    assert seen["rail"] == RAIL_CONSUMER
    sent = "\n".join(c.args[1] for c in bot.application.bot.send_message.await_args_list)
    assert "RAMP_DECISION" not in sent and "<pre>" in sent


# --- R4: dashboard pin ---

def test_r4_dashboard_pin_render_declares_consumer_rail():
    from bridge import dashboard
    with patch("bridge.dashboard.sanitize_message_for_telegram", return_value="ok") as san:
        out = dashboard.DashboardSync._render_pin_html(object(), "x")
    assert out == "ok"
    assert san.call_args.kwargs.get("rail") == RAIL_CONSUMER


# --- R3: push.sh sanitize hop (the hop's inline python, run for real) ---

def _extract_push_hop():
    src = PUSH_SH.read_text(encoding="utf-8")
    m = re.search(r'SANITIZED="\$\(printf \'%s\' "\$BODY" \| "\$_PUSH_PYTHON" -c "\n(.*?)\n" "\$\(cd "\$BRIDGE_DIR/\.\." && pwd\)" "\$\(cd "\$SCRIPT_DIR/\.\." && pwd\)"', src, re.S)
    assert m, "push.sh sanitize hop not found in expected shape"
    # Undo the shell double-quote escaping the heredoc-in-string needs.
    return m.group(1).replace('\\"', '"').replace("\\\\n", "\\n")


def _run_hop(body: str, root: Path, gated_file: Path = None,
             alerts_file: Path = None, extra_env: dict = None):
    code = _extract_push_hop()
    argv = [sys.executable, "-c", code, str(TEMPLATE_DIR), str(root)]
    if gated_file is not None:
        argv.append(str(gated_file))
    if alerts_file is not None:
        assert gated_file is not None, "alerts spool is argv[4]; gated file is argv[3]"
        argv.append(str(alerts_file))
    env = {"PROJECT_ROOT": str(root), "PATH": "/usr/bin:/bin", "TELEGRAM_BOT_TOKEN": "test:token",
           "BRIDGE_MACHINE_LINE_GATE": "1"}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        argv,
        input=body.encode("utf-8"), capture_output=True, timeout=30,
        env=env,
    )


@requires_push
def test_r3_push_hop_drops_leak_keeps_table(tmp_path):
    (tmp_path / ".telegram_bot").mkdir()
    p = _run_hop(f"{TABLE}\n{LEAK_DAY_SOURCE}\n{LEAK_RAMP}\n", tmp_path)
    assert p.returncode == 0, p.stderr.decode()
    out = p.stdout.decode()
    assert "DAY_SOURCE" not in out and "RAMP_DECISION" not in out
    assert "<pre>" in out


@requires_push
def test_r3_push_hop_writes_gated_raw_for_the_http400_plain_fallback(tmp_path):
    (tmp_path / ".telegram_bot").mkdir()
    gated = tmp_path / "gated.txt"
    p = _run_hop(f"{TABLE}\n{LEAK_DAY_SOURCE}\n{LEAK_RAMP}\n{NORMAL_KO}\n", tmp_path, gated)
    assert p.returncode == 0, p.stderr.decode()
    text = gated.read_text(encoding="utf-8")
    assert text == f"{TABLE}\n{NORMAL_KO}\n"          # raw (unrendered) but gated
    src = PUSH_SH.read_text(encoding="utf-8")
    assert 'RAW_BODY="$(cat "$_GATED_RAW_FILE")"' in src   # the fallback uses it


@requires_push
def test_r3_push_hop_machine_only_exits_3_so_raw_fallback_is_skipped(tmp_path):
    (tmp_path / ".telegram_bot").mkdir()
    p = _run_hop(f"{LEAK_DAY_SOURCE}\n{LEAK_RAMP}\n", tmp_path)
    assert p.returncode == 3, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == b""
    src = PUSH_SH.read_text(encoding="utf-8")
    assert '[[ "$_SAN_RC" -eq 3 ]]' in src           # the shell branch that honors it


@requires_push
def test_r3_push_hop_unregistered_shape_spools_alert_once_per_day(tmp_path):
    """DGN-1589/DGN-1591: the alert is NEVER appended to the recipient's
    body (that put English internals on end-user screens). Fresh hits go to
    the argv[4] spool file for the shell-side diagnostic logging; dedup per
    (rail, token, day) still holds."""
    (tmp_path / ".telegram_bot").mkdir()
    body = "루틴 결과입니다\nBRAND_NEW_SIGNAL k=v\n"
    gated = tmp_path / "gated.txt"
    spool1 = tmp_path / "alerts1.txt"
    p1 = _run_hop(body, tmp_path, gated, spool1)
    assert p1.returncode == 0, p1.stderr.decode()
    out1 = p1.stdout.decode()
    assert "BRAND_NEW_SIGNAL k=v" in out1                    # passed through
    assert out1.count("BRAND_NEW_SIGNAL") == 1               # no trailer on the body
    assert spool1.read_text(encoding="utf-8").strip() == "consumer\tBRAND_NEW_SIGNAL"
    markers = list((tmp_path / ".telegram_bot" / "machine-line-alerts").iterdir())
    assert len(markers) == 1 and markers[0].name.startswith("consumer.BRAND_NEW_SIGNAL.")
    spool2 = tmp_path / "alerts2.txt"
    p2 = _run_hop(body, tmp_path, gated, spool2)
    out2 = p2.stdout.decode()
    assert out2.count("BRAND_NEW_SIGNAL") == 1               # still passes
    assert not spool2.exists()                               # dedup'd: nothing spooled


# ESTATE:BEGIN the legacy alert-hop knob is Dogany-named (DOGANY_MACHINE_ALERT_HOP)
@requires_push
def test_r3_push_hop_alert_hop_env_disables_alerting(tmp_path):
    """Legacy DOGANY_MACHINE_ALERT_HOP=1 callers suppress diagnostics:
    the body must never spool a new alert and
    must not burn a ledger marker."""
    (tmp_path / ".telegram_bot").mkdir()
    body = "루틴 결과입니다\nBRAND_NEW_SIGNAL k=v\n"
    gated = tmp_path / "gated.txt"
    spool = tmp_path / "alerts.txt"
    p = _run_hop(body, tmp_path, gated, spool,
                 extra_env={"DOGANY_MACHINE_ALERT_HOP": "1"})
    assert p.returncode == 0, p.stderr.decode()
    assert "BRAND_NEW_SIGNAL k=v" in p.stdout.decode()       # gate still passes text
    assert not spool.exists()
    assert not (tmp_path / ".telegram_bot" / "machine-line-alerts").exists()
# ESTATE:END


@requires_push
def test_push_sh_spooled_alerts_have_no_delivery_route():
    src = PUSH_SH.read_text(encoding="utf-8")
    start = src.index('if [[ -s "$_ALERT_HITS_FILE"')
    end = src.index('rm -f "$_ALERT_HITS_FILE"', start)
    route = src[start:end]
    assert "ledger only" in route
    assert "push.sh" not in route and "STEWARD_ENV_FILE" not in route
    assert "session-inbox" not in route


# --- source-level wiring lint (same predicates as git-hooks/pre-push) ---

@requires_push
def test_every_rail_call_site_declares_its_rail_in_source():
    bot_src = (BRIDGE_DIR / "bot.py").read_text(encoding="utf-8")
    fmt_src = (BRIDGE_DIR / "formatting.py").read_text(encoding="utf-8")
    dash_src = (BRIDGE_DIR / "dashboard.py").read_text(encoding="utf-8")
    push_src = PUSH_SH.read_text(encoding="utf-8")
    assert "content = apply_machine_line_gate(content, rail)" in bot_src      # bot choke point
    assert "text = apply_machine_line_gate(text, rail)" in fmt_src            # formatting choke point
    # ESTATE:BEGIN the R1 call site lives inside the bot.py idrill engine region; the public tree has no idrill followup rail
    assert "text, rail=RAIL_CONSUMER" in bot_src                               # R1
    # ESTATE:END
    assert "force_options=has_options, rail=RAIL_CONSUMER" in bot_src          # R2
    assert "rail=machine_gate.RAIL_CONSUMER" in push_src                       # R3
    assert "sanitize_message_for_telegram(text, rail=RAIL_CONSUMER)" in dash_src  # R4
    # model-turn sends declare model (log-only alerts) -- the reply body too.
    assert bot_src.count("rail=RAIL_MODEL") >= 6
    assert "self._render_prose_html_segments(content, rail=RAIL_MODEL)" in bot_src
    # No stray inline render pipeline left in _send_text_body (single choke point).
    body = bot_src.split("async def _send_text_body(")[1].split("async def _try_send_linked")[0]
    assert "split_into_segments" not in body


def test_pre_push_hook_carries_the_wiring_lint_and_copies_are_identical():
    root = TEMPLATE_DIR.parent.parent
    hook = root / "git-hooks" / "pre-push"
    tmpl = TEMPLATE_DIR / "git-hooks" / "pre-push"
    if not hook.exists():
        pytest.skip("canonical repo layout not present")
    src = hook.read_text(encoding="utf-8")
    assert "DGN-1209" in src and "apply_machine_line_gate" in src
    assert hook.read_bytes() == tmpl.read_bytes()   # DGN-1058 single variant


# ===========================================================================
# E. emitter helper -- the ONE way to print a signal; sigil == bridge constant
# ===========================================================================

def _load_helper():
    spec = importlib.util.spec_from_file_location("machine_signal", HELPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@requires_helper
def test_helper_sigil_matches_bridge_marker():
    assert _load_helper().SIGIL == MACHINE_SIGNAL_MARKER == "signal::"


@requires_helper
def test_helper_output_is_registered_by_construction():
    ms = _load_helper()
    kv = ms.signal("RAMP_DECISION", exercise="squat", decision="ramp", rungs=2)
    say = ms.say("LOAD_DECISION", "8회 성공이라\n2.5kg 올렸습니다")
    assert kv == "signal::RAMP_DECISION exercise=squat decision=ramp rungs=2"
    assert say == "signal::LOAD_DECISION: 8회 성공이라 2.5kg 올렸습니다"   # one line
    calls = _capture_sink()
    out = apply_machine_line_gate(f"{TABLE}\n{kv}\n{say}\n", RAIL_CONSUMER)
    assert out == f"{TABLE}\n8회 성공이라 2.5kg 올렸습니다\n"
    assert calls == []


@requires_helper
def test_helper_rejects_non_token_names():
    ms = _load_helper()
    with pytest.raises(ValueError):
        ms.signal("lowercase")
    with pytest.raises(ValueError):
        ms.say("1BAD", "x")
    assert ms.say("EMPTY_SAY", "   ") == "signal::EMPTY_SAY"   # never a blank line


@requires_helper
def test_helper_cli_prints_wire_format():
    p = subprocess.run([sys.executable, str(HELPER_PATH), "DAY_SOURCE", "exercise=a", "day=뒷B"],
                       capture_output=True, timeout=30)
    assert p.returncode == 0 and p.stdout.decode() == "signal::DAY_SOURCE exercise=a day=뒷B\n"
    p = subprocess.run([sys.executable, str(HELPER_PATH), "--say", "LOAD_DECISION", "8회", "성공"],
                       capture_output=True, timeout=30)
    assert p.stdout.decode() == "signal::LOAD_DECISION: 8회 성공\n"
    p = subprocess.run([sys.executable, str(HELPER_PATH), "bad-token"], capture_output=True, timeout=30)
    assert p.returncode == 2


# ===========================================================================
# F. copy + marker registration
# ===========================================================================

def test_alert_copy_present_in_both_catalogs_with_placeholders():
    for cat in (en.STRINGS, ko.STRINGS):
        assert "{tokens}" in cat["machine_line_alert"] and "{rail}" in cat["machine_line_alert"]
        assert cat["machine_line_alert_undeclared"].strip()


def test_sigil_is_a_recognized_colon_marker():
    # The containment net must not ZWSP-break a stray sigil line into prose;
    # the gate owns it (drop/strip) before any render.
    assert MACHINE_SIGNAL_MARKER in RECOGNIZED_COLON_MARKERS


# ===========================================================================
# G. bot-side reader -- DGN-1756: durable query-time diagnostics, with or
# without a steward. No chat send and no session-inbox injection.
# ===========================================================================

def _reader_bot(tmp_path):
    from bridge.bot import TelegramBot
    bot = TelegramBot.__new__(TelegramBot)
    bot._machine_alert_ledger = MachineLineAlertLedger(tmp_path / "ml")
    bot._machine_alert_inflight = set()
    bot.application = MagicMock()
    bot.application.bot.send_message = AsyncMock()
    return bot


def _instance(tmp_path, steward_env: str = None):
    """Sandbox instance root: .telegram_bot data dir (+ optional agent.conf
    with a resolvable STEWARD_ENV_FILE)."""
    data_dir = tmp_path / ".telegram_bot"
    data_dir.mkdir(exist_ok=True)
    if steward_env is not None:
        conf_dir = tmp_path / "config"
        conf_dir.mkdir(exist_ok=True)
        (conf_dir / "agent.conf").write_text(
            "STEWARD_ENV_FILE=%s\n" % steward_env, encoding="utf-8"
        )
    return data_dir


@pytest.mark.parametrize("steward_setting", [None, "", "/nonexistent/steward.env", "configured"])
def test_reader_zero_owner_chat_and_zero_session_inbox(tmp_path, steward_setting, caplog):
    """Any steward configuration: durable record, zero delivery or injection."""
    bot = _reader_bot(tmp_path)
    if steward_setting == "configured":
        steward = tmp_path / "steward.env"
        steward.write_text("# Resolvable steward configuration; never read or sent.\n")
        steward_setting = str(steward)
    data_dir = _instance(tmp_path, steward_env=steward_setting)

    async def scenario():
        with patch.object(config_mod.config, "allowed_user_ids", [42]), \
             patch.object(config_mod.config, "bot_data_dir", data_dir), \
             patch.object(mg, "_alert_sink", bot._machine_line_alert_sink), \
             patch.object(asyncio, "create_subprocess_exec", new_callable=AsyncMock) as spawn:
            mg.apply_machine_line_gate("FOO_BAR k=v", RAIL_CONSUMER)
            mg.apply_machine_line_gate("FOO_BAR k=v", RAIL_CONSUMER)  # in-flight dedup
            await asyncio.sleep(0)
            await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
            mg.apply_machine_line_gate("FOO_BAR k=v", RAIL_CONSUMER)  # ledger dedup
            await asyncio.sleep(0)
            spawn.assert_not_awaited()
    asyncio.run(scenario())

    assert bot.application.bot.send_message.await_count == 0   # owner surface: zero
    assert not (data_dir / "session-inbox").exists()
    assert "ledger only" in caplog.text
    markers = list(bot._machine_alert_ledger.directory.iterdir())
    assert len(markers) == 1
    assert markers[0].name == "consumer.FOO_BAR." + mg.today_key()
    assert markers[0].read_text() == mg.today_key() + "\n"
    # A fresh reader/process sees the durable record too.
    assert mg.MachineLineAlertLedger(bot._machine_alert_ledger.directory).unseen(
        RAIL_CONSUMER, ["FOO_BAR"]) == []
    assert bot._machine_alert_ledger.unseen(RAIL_CONSUMER, ["FOO_BAR"]) == []
    assert bot._machine_alert_inflight == set()


def test_reader_route_is_independent_of_owner_chat(tmp_path):
    """The old sink required an owner chat to reach; the operator route must
    not -- an instance with no allowed user still records the alert."""
    bot = _reader_bot(tmp_path)
    data_dir = _instance(tmp_path)

    async def scenario():
        with patch.object(config_mod.config, "allowed_user_ids", []), \
             patch.object(config_mod.config, "bot_data_dir", data_dir):
            bot._machine_line_alert_sink(RAIL_CONSUMER, ("FOO_BAR",), "x")
            await asyncio.sleep(0)
            await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
    asyncio.run(scenario())
    assert bot.application.bot.send_message.await_count == 0
    assert not (data_dir / "session-inbox").exists()
    assert bot._machine_alert_ledger.unseen(RAIL_CONSUMER, ["FOO_BAR"]) == []


def test_reader_is_installed_by_bot_constructor_source():
    # The sink is wired in __init__ (not in a start hook a refactor can skip).
    src = (BRIDGE_DIR / "bot.py").read_text(encoding="utf-8")
    init = src.split("    def __init__(self) -> None:")[1].split("\n    def ")[0]
    assert "set_machine_line_alert_sink(self._machine_line_alert_sink)" in init


@requires_push
@pytest.mark.parametrize("configured", [False, True])
def test_push_zero_owner_send_and_zero_session_inbox(tmp_path, configured):
    """Run only the extracted alert routing block, never push.sh or a live send."""
    spool = tmp_path / "alerts.txt"
    result = _run_hop("NEW_EMITTER k=v", tmp_path, tmp_path / "gated", spool)
    assert result.returncode == 0
    src = PUSH_SH.read_text()
    start = src.index('if [[ -s "$_ALERT_HITS_FILE"')
    end = src.index('rm -f "$_ALERT_HITS_FILE"', start)
    steward = tmp_path / "steward.env"
    steward.write_text("# Resolvable steward configuration; never read or sent.\n")
    _instance(tmp_path, str(steward) if configured else None)
    script = ('read_kv() { printf "%s" "$STEWARD_ENV_FILE"; }\n'
              'bash() { echo unexpected-send; return 99; }\n'
              # ESTATE:BEGIN the legacy alert-hop knob is Dogany-named
              'export -n DOGANY_MACHINE_ALERT_HOP\nunset DOGANY_MACHINE_ALERT_HOP\n'
              # ESTATE:END
              '_ALERT_HITS_FILE="$1"\nSCRIPT_DIR="$2"\n' + src[start:end])
    routed = subprocess.run(["bash", "-c", script, "test", str(spool), str(tmp_path)],
                            capture_output=True, text=True, timeout=30,
                            env={"PATH": os.environ["PATH"],
                                 "STEWARD_ENV_FILE": str(steward) if configured else ""})
    assert routed.returncode == 0, routed.stderr
    assert routed.stdout == ""
    assert "ledger only" in routed.stderr
    assert not (tmp_path / ".telegram_bot" / "session-inbox").exists()
    ledger = mg.MachineLineAlertLedger(tmp_path / ".telegram_bot" / "machine-line-alerts")
    assert ledger.unseen(RAIL_CONSUMER, ["NEW_EMITTER"]) == []


# ESTATE:BEGIN /health reader -- healthcmd.py is manifest-excluded, canonical-only
def test_health_exposes_persisted_machine_discovery_only_as_technical_detail(tmp_path):
    from datetime import datetime
    from bridge import healthcmd
    ledger = mg.MachineLineAlertLedger(tmp_path / ".telegram_bot" / "machine-line-alerts")
    ledger.mark(RAIL_CONSUMER, ["NEW_EMITTER"])
    before = {p.name: p.read_bytes() for p in ledger.directory.iterdir()}
    inst = healthcmd.assess_instance(
        display="테스트", root=str(tmp_path), label="test", launchd_pid=123,
        lstart=None, shared_env_path=None, plist_path=None, now=datetime.now())
    lines = [line for line in inst["tech"] if "기계어 발견 기록" in line]
    assert len(lines) == 1 and "1건" in lines[0] and "NEW_EMITTER" in lines[0]
    assert all("NEW_EMITTER" not in str(issue) for issue in inst["issues"])
    report = {"now": datetime.now(), "self": inst, "cli": {}, "estate": [], "jobs": {}}
    text, ctas = healthcmd.compose_health_report(report)
    assert "NEW_EMITTER" in text
    assert all("NEW_EMITTER" not in cta for cta in ctas)
    assert before == {p.name: p.read_bytes() for p in ledger.directory.iterdir()}
# ESTATE:END


@pytest.mark.parametrize("label", [
    "목표", "이번 주", "오늘", "진행중 티켓", "백그라운드 작업 중",
    "Résumé", "今週", "이번 주 계획 3개", "오늘 운동",
])
@pytest.mark.parametrize("rail", [RAIL_CONSUMER, RAIL_MODEL, RAIL_UNKNOWN])
def test_natural_language_bracket_headers_are_owner_copy(label, rail):
    text = f"[{label}]\n할 일을 확인하세요."
    calls = []
    mg.set_machine_line_alert_sink(lambda *args: calls.append(args))
    assert apply_machine_line_gate(text, rail) == text
    assert gate_machine_lines(text, rail).unregistered == ()
    assert calls == []


@pytest.mark.parametrize("text", [
    "[pack lag]", "[FOO_BAR]", "[worker_id]", "[123]", "[🔥]",
    "[목표 FOO_BAR]", "[작업 worker_id]", "[목표 signal::FOO]",
    "[작업 dsp-20260927-164715-5044]", "[목표 age=123]",
    "[목표] age=123", "[목표] signal::FOO", "[목표] dsp-20260927-164715-5044",
])
def test_bracket_machine_shapes_still_alert(text):
    result = gate_machine_lines(text, RAIL_CONSUMER)
    assert result.text == text
    assert result.unregistered


@requires_push
def test_push_hop_owner_sections_create_no_diagnostic(tmp_path):
    text = "[목표]\n운동하기\n[이번 주]\n세 번\n[오늘]\n걷기"
    spool = tmp_path / "alerts"
    gated = tmp_path / "gated"
    result = _run_hop(text, tmp_path, gated, spool)
    assert result.returncode == 0
    assert gated.read_text() == text
    assert not spool.exists()
    assert not (tmp_path / ".telegram_bot" / "machine-line-alerts").exists()
