"""DGN-1500: layered term dictionary (framework / kit / module) at the send seat.

The dictionary is DATA in the instance (config/terms/framework.json +
config/packs/<pack-id>.terms.json), loaded by routines/lib/term_dict.py and
matched by output_gate TERM_LEAK; the bridge send seat counts every hit in
the output-gate canary log and rewrites only safe-to-rewrite entries. It is
never injected into model context.

Acceptance (ticket + leader decisions):
  1. framework leak (bridge -> the Korean "leg/bridge" word) is counted in a
     dev agent
  2. the same word is NOT counted in an agent whose module layer allows it
     (health: legs), nor where a layer switches the entry off
  3. a safe-to-rewrite entry is rewritten AND counted
  4. an unsafe entry is counted only
  5. kill switch off -> untouched but counted
  6. missing / broken dictionary -> message untouched
Plus: resolution order + conflicts logged once, masking, word boundaries,
rewrite-safety refusals, and the two send seats.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge import artifacts  # noqa: E402

TEMPLATE = _root
LIB_DIR = TEMPLATE / "routines" / "lib"
SWITCH_PATH = TEMPLATE / "routines" / "gate-switch.py"
FRAMEWORK_DICT = TEMPLATE / "config" / "terms" / "framework.json"
REPO = TEMPLATE.parents[1]
HANDOVER = REPO / "kit-handover"
pytestmark = pytest.mark.skipif(
    not ((LIB_DIR / "term_dict.py").is_file()
         and (LIB_DIR / "output_gate.py").is_file()
         and SWITCH_PATH.is_file() and FRAMEWORK_DICT.is_file()),
    reason="routines/lib term dictionary not beside this bridge tree",
)

# Real sentences from the 2026-09-15 incident / the 2026-09-30 replay.
LEAK = "다리 재시작은 뺐습니다."           # bridge restart, mistranslated
LEGS = "세션마다 한쪽 다리로 하는 동작을 넣어요."  # legs, plain meaning

HEALTH_LAYER = {
    "format": "dogany-term-dict/1",
    "allow": ["다리"],
    "entries": [
        {"id": "warmup", "canonical": "웜업", "forbidden": ["워밍업"],
         "rewrite": True, "approved": "owner 2026-08-26"},
        {"id": "split", "canonical": "분할", "forbidden": ["스플릿"],
         "rewrite": False, "approved": "owner 2026-09-16",
         "except": ["스플릿 스쿼트"]},
    ],
}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def td():
    return _load("term_dict_dgn1500", LIB_DIR / "term_dict.py")


@pytest.fixture
def og():
    return _load("output_gate_dgn1500", LIB_DIR / "output_gate.py")


def _instance(root: Path, framework=True):
    (root / "routines" / "lib").mkdir(parents=True)
    for name in ("term_dict.py", "output_gate.py"):
        shutil.copy(LIB_DIR / name, root / "routines" / "lib" / name)
    shutil.copy(SWITCH_PATH, root / "routines" / "gate-switch.py")
    (root / "config" / "packs").mkdir(parents=True)
    (root / "config" / "agent.conf").write_text("", encoding="utf-8")
    if framework:
        (root / "config" / "terms").mkdir()
        shutil.copy(FRAMEWORK_DICT, root / "config" / "terms" / "framework.json")
    return root


def _layer(root: Path, pack_id: str, layer: str, data: dict):
    body = dict(data, layer=layer, owner=pack_id)
    (root / "config" / "packs" / (pack_id + ".terms.json")).write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_switch_env(monkeypatch, tmp_path):
    for key in ("GATE_SEND_OUTPUT_TERM_DICT", "GATE_SEND_ALL", "GATE_STOP_ALL",
                "GATE_ALL", "GATE_SWITCH_CONF"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GATE_SWITCH_LOG", str(tmp_path / "suppressed.log"))


@pytest.fixture
def dev_agent(tmp_path):
    """Framework layer only (a dev / canonical-manager agent)."""
    return _instance(tmp_path / "dev")


@pytest.fixture
def health_agent(tmp_path):
    """Framework + kit_life (kit) + pack_health (module, allows legs)."""
    root = _instance(tmp_path / "health")
    _layer(root, "kit_life", "kit", {"format": "dogany-term-dict/1",
                                     "entries": []})
    _layer(root, "pack_health", "module", HEALTH_LAYER)
    return root


def _canary(logs: Path):
    p = logs / artifacts.OUTPUT_GATE_CANARY_LOG
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _leaks(logs):
    return [ln for ln in _canary(logs) if "  TERM_LEAK  " in ln]


def _gate(root: Path, text: str):
    logs = root / "logs"
    out = artifacts.normalize_term_leaks(text, root, log_dir=logs)
    return out, _leaks(logs), _canary(logs)


# ---------------------------------------------------------------------------
# Acceptance 1-2: scoping and per-layer override
# ---------------------------------------------------------------------------

def test_1_framework_leak_counted_in_dev_agent(dev_agent):
    (out, changed), leaks, _ = _gate(dev_agent, LEAK)
    assert (out, changed) == (LEAK, False)          # counted, never rewritten
    assert len(leaks) == 1
    ln = leaks[0]
    assert "stage=counted" in ln and "layer=framework" in ln
    assert "id=bridge" in ln and "form=다리" in ln and "[%s]" % LEAK in ln


def test_2_same_word_not_counted_where_module_allows_it(health_agent):
    (out, changed), leaks, lines = _gate(health_agent, LEAK)
    assert (out, changed) == (LEAK, False) and leaks == []
    # the veto is a load-time note, logged once
    vetoes = [ln for ln in lines if "kind=veto" in ln]
    assert len(vetoes) == 1 and "id=bridge" in vetoes[0]
    assert "pack_health" in vetoes[0]


def test_2_entry_switched_off_by_kit_layer(dev_agent):
    _layer(dev_agent, "kit_x", "kit", {
        "format": "dogany-term-dict/1",
        "entries": [{"id": "bridge", "off": True}]})
    (out, changed), leaks, lines = _gate(dev_agent, LEAK)
    assert not changed and leaks == []
    assert any("kind=override" in ln and "disables" in ln for ln in lines)


def test_2_plain_meaning_not_counted_even_without_module_layer(dev_agent):
    # the framework entry is cue-gated: legs in workout talk has no runtime cue
    (out, changed), leaks, _ = _gate(dev_agent, LEGS)
    assert not changed and leaks == []


def test_resolution_most_specific_wins(td, tmp_path):
    root = _instance(tmp_path / "r")
    common = {"canonical": "A", "rewrite": False}
    _layer(root, "kit_a", "kit", {"format": "dogany-term-dict/1", "entries": [
        dict(common, id="bridge", forbidden=["교각"])]})
    _layer(root, "pack_a", "module", {"format": "dogany-term-dict/1", "entries": [
        dict(common, id="bridge", forbidden=["연결다리"])]})
    notes = []
    rules = td.load(root, log=lambda k, d: notes.append((k, d)))
    bridge = [r for r in rules if r["id"] == "bridge"][0]
    assert bridge["layer"] == "module" and bridge["forms"] == ["연결다리"]
    assert [k for k, _ in notes].count("override") == 2  # kit>fw, module>kit


def test_same_rank_conflict_first_owner_wins_and_is_logged(td, tmp_path):
    root = _instance(tmp_path / "c", framework=False)
    e = {"id": "x", "canonical": "C", "rewrite": False}
    _layer(root, "kit_b", "kit", {"format": "dogany-term-dict/1",
                                  "entries": [dict(e, forbidden=["비"])]})
    _layer(root, "kit_a", "kit", {"format": "dogany-term-dict/1",
                                  "entries": [dict(e, forbidden=["에이"])]})
    notes = []
    rules = td.load(root, log=lambda k, d: notes.append((k, d)))
    assert rules[0]["owner"] == "kit_a" and rules[0]["forms"] == ["에이"]
    assert [k for k, _ in notes] == ["conflict"]


def test_load_notes_logged_once_per_dictionary_state(health_agent):
    logs = health_agent / "logs"
    for _ in range(3):
        artifacts.normalize_term_leaks(LEAK, health_agent, log_dir=logs)
    assert len([ln for ln in _canary(logs) if "kind=veto" in ln]) == 1
    # a changed layer file is a new state -> its notes are logged again
    p = health_agent / "config" / "packs" / "pack_health.terms.json"
    p.write_text(p.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    artifacts.normalize_term_leaks(LEAK, health_agent, log_dir=logs)
    assert len([ln for ln in _canary(logs) if "kind=veto" in ln]) == 2


# ---------------------------------------------------------------------------
# Acceptance 3-5: rewrite-safe vs unsafe, kill switch
# ---------------------------------------------------------------------------

def test_3_safe_entry_rewritten_and_counted(health_agent):
    text = "워밍업은 가볍게, 워밍업 2세트 하고 들어갈게요."
    (out, changed), leaks, _ = _gate(health_agent, text)
    assert changed and out == "웜업은 가볍게, 웜업 2세트 하고 들어갈게요."
    assert len(leaks) == 2
    assert all("stage=rewritten" in ln and "layer=module" in ln
               and "owner=pack_health" in ln and "id=warmup" in ln
               for ln in leaks)


def test_4_unsafe_entry_counted_only(health_agent):
    text = "이번 주는 스플릿 루틴으로 갑니다."
    (out, changed), leaks, _ = _gate(health_agent, text)
    assert (out, changed) == (text, False)
    assert len(leaks) == 1 and "stage=counted" in leaks[0]
    assert "id=split" in leaks[0]


def test_5_kill_switch_off_untouched_but_counted(health_agent, monkeypatch,
                                                  tmp_path):
    monkeypatch.setenv("GATE_SEND_OUTPUT_TERM_DICT", "off")
    text = "워밍업 2세트 하고 스플릿 루틴으로 갑니다."
    (out, changed), leaks, _ = _gate(health_agent, text)
    assert (out, changed) == (text, False)
    stages = sorted(ln.split("stage=")[1].split()[0] for ln in leaks)
    assert stages == ["counted", "suppressed-off"]
    sup = (tmp_path / "suppressed.log").read_text(encoding="utf-8")
    assert "output-gate-term-dict" in sup and "워밍업->웜업" in sup


def test_5_send_master_off_and_other_masters_do_not_disarm(health_agent,
                                                            monkeypatch):
    monkeypatch.setenv("GATE_STOP_ALL", "off")
    monkeypatch.setenv("GATE_ALL", "off")
    (out, changed), _, _ = _gate(health_agent, "워밍업 끝.")
    assert changed and out == "웜업 끝."
    (health_agent / "config" / "agent.conf").write_text(
        "GATE_SEND_ALL=off\n", encoding="utf-8")
    (out, changed), _, _ = _gate(health_agent, "워밍업 끝.")
    assert not changed and out == "워밍업 끝."


# ---------------------------------------------------------------------------
# Acceptance 6: missing / broken dictionary
# ---------------------------------------------------------------------------

def test_6_missing_dictionary_untouched(tmp_path):
    root = _instance(tmp_path / "none", framework=False)
    (out, changed), leaks, lines = _gate(root, LEAK)
    assert (out, changed) == (LEAK, False) and lines == []


def test_6_broken_framework_file_untouched(dev_agent):
    (dev_agent / "config" / "terms" / "framework.json").write_text(
        "{not json", encoding="utf-8")
    (out, changed), leaks, lines = _gate(dev_agent, LEAK)
    assert (out, changed) == (LEAK, False) and leaks == []
    assert len([ln for ln in lines if "kind=broken" in ln]) == 1


def test_6_broken_module_layer_disables_whole_dictionary(health_agent):
    # ignoring only the broken layer would drop its veto and count legs
    (health_agent / "config" / "packs" / "pack_health.terms.json").write_text(
        json.dumps({"format": "dogany-term-dict/1", "layer": "module",
                    "owner": "pack_health", "entries": [{"id": "BAD"}]}),
        encoding="utf-8")
    (out, changed), leaks, lines = _gate(health_agent, "워밍업. " + LEAK)
    assert not changed and leaks == []
    assert any("kind=broken" in ln for ln in lines)


def test_6_layer_claiming_wrong_owner_or_layer_is_broken(td, tmp_path):
    root = _instance(tmp_path / "w", framework=False)
    (root / "config" / "packs" / "kit_a.terms.json").write_text(json.dumps(
        {"format": "dogany-term-dict/1", "layer": "framework",
         "owner": "kit_a", "entries": []}), encoding="utf-8")
    assert td.load(root) is None


def test_6_lib_absent_untouched(tmp_path):
    root = tmp_path / "bare"
    (root / "config" / "terms").mkdir(parents=True)
    shutil.copy(FRAMEWORK_DICT, root / "config" / "terms" / "framework.json")
    out = artifacts.normalize_term_leaks(LEAK, root, log_dir=root / "logs")
    assert out == (LEAK, False)


# ---------------------------------------------------------------------------
# Matcher (output_gate TERM_LEAK)
# ---------------------------------------------------------------------------

def _rules(td, root):
    return td.load(root)


def test_word_boundary_and_particles(td, og, health_agent):
    rules = _rules(td, health_agent)
    hit = lambda t: [h["match"] for h in og.find_term_leaks(t, rules)]  # noqa: E731
    assert hit("워밍업을 했어요.") == ["워밍업"]
    assert hit("워밍업에서는 가볍게.") == ["워밍업"]
    assert hit("프리워밍업 세트") == []          # inside a longer word
    assert hit("워밍업자 모임") == []            # not a particle


def test_ladder_is_not_bridge(td, og, dev_agent):
    rules = _rules(td, dev_agent)
    assert og.find_term_leaks("사다리 재시작 코드", rules) == []


def test_quotes_blockquotes_inline_code_masked(td, og, dev_agent):
    rules = _rules(td, dev_agent)
    for text in ('형님 말씀 "다리 재시작" 그대로요.',
                 "> 다리 재시작은 뺐습니다.",
                 "`다리 재시작` 이라는 로그 키"):
        assert og.find_term_leaks(text, rules) == [], text


def test_fence_counted_never_rewritten(health_agent):
    # the 2026-09-15 incident sat in a fenced status table
    text = "```\n다리 3대 기동     9월 14일\n워밍업 2세트\n```"
    (out, changed), leaks, _ = _gate(health_agent, text)
    assert (out, changed) == (text, False)
    assert len(leaks) == 1 and "stage=counted" in leaks[0]   # warmup only:
    dev = health_agent.parent / "dev2"                        # legs allowed
    _instance(dev)
    (out, changed), leaks, _ = _gate(dev, text)
    assert not changed and len(leaks) == 1 and "id=bridge" in leaks[0]


def test_rewrite_refusals_are_counted_only(td, tmp_path):
    root = _instance(tmp_path / "rr", framework=False)
    _layer(root, "pack_z", "module", {"format": "dogany-term-dict/1", "entries": [
        {"id": "no-approval", "canonical": "웜업", "forbidden": ["웜엎"],
         "rewrite": True},
        {"id": "batchim", "canonical": "할 일", "forbidden": ["태스크"],
         "rewrite": True, "approved": "owner"},
        {"id": "cue", "canonical": "분할", "forbidden": ["스플릿"],
         "rewrite": True, "approved": "owner", "context_any": ["루틴"]},
    ]})
    notes = []
    rules = td.load(root, log=lambda k, d: notes.append((k, d)))
    assert all(not r["rewrite"] for r in rules)
    assert sorted(d.split()[0] for k, d in notes if k == "rewrite-refused") == [
        "id=batchim", "id=cue", "id=no-approval"]


def test_except_phrase_is_not_a_leak(health_agent):
    # replay 2026-09-30: every split hit was the exercise name
    text = "불가리안 스플릿 스쿼트 3세트 들어갑니다."
    (out, changed), leaks, _ = _gate(health_agent, text)
    assert not changed and leaks == []


def test_contrast_sentence_counted_not_rewritten(health_agent):
    # "A, not B" must not become "A, not A"
    text = "표기는 웜업이고 워밍업이 아닙니다. 워밍업 끝."
    (out, changed), leaks, _ = _gate(health_agent, text)
    assert changed and out == "표기는 웜업이고 워밍업이 아닙니다. 웜업 끝."
    stages = [ln.split("stage=")[1].split()[0] for ln in leaks]
    assert stages == ["counted", "rewritten"]


def test_find_is_identity_safe_on_empty(og):
    assert og.find_term_leaks("", [{"id": "x"}]) == []
    assert og.find_term_leaks("abc", None) == []
    assert og.apply_term_rewrites("abc", []) == "abc"


# ---------------------------------------------------------------------------
# Shipped seeds
# ---------------------------------------------------------------------------

def test_framework_seed_loads_clean_and_is_count_only(td, dev_agent):
    notes = []
    rules = td.load(dev_agent, log=lambda k, d: notes.append((k, d)))
    assert notes == []
    assert {r["id"] for r in rules} == {"bridge", "kit", "pack", "module", "skill"}
    assert not any(r["rewrite"] for r in rules)


@pytest.mark.skipif(not HANDOVER.is_dir(), reason="no kit-handover/ in this tree")
def test_handover_seeds_validate_as_their_layers(td, tmp_path):
    root = _instance(tmp_path / "seed")
    for pid, layer in (("kit_life", "kit"), ("pack_health", "module")):
        data = json.loads((HANDOVER / pid / "payload" / "config" / "terms.json")
                          .read_text(encoding="utf-8"))
        _layer(root, pid, layer, data)
    notes = []
    rules = td.load(root, log=lambda k, d: notes.append((k, d)))
    assert rules is not None
    assert [r["id"] for r in rules if r["rewrite"]] == ["warmup"]
    assert [k for k, _ in notes] == ["veto"]     # legs vetoes the bridge form


# ---------------------------------------------------------------------------
# Send seats (bot.py)
# ---------------------------------------------------------------------------

def _load_bot():
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)
    import bridge.bot as bot_mod
    return bot_mod


@pytest.fixture
def seat(health_agent, monkeypatch, tmp_path):
    bot_mod = _load_bot()
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", health_agent)
    logs = tmp_path / "botlogs"
    monkeypatch.setattr(bot_mod.config, "logs_dir", logs)
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    bot.application.bot = MagicMock()
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = {}
    bot._send_content_artifacts = AsyncMock()
    bot._kick_update_offer = MagicMock()
    return bot_mod, bot, logs


def _message(sent):
    msg = MagicMock()
    msg.chat.id = 42
    msg.message_id = 100
    from datetime import datetime, timezone
    msg.date = datetime.now(timezone.utc)

    async def _reply_text(text, **kw):
        sent.append(text)

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    tg = MagicMock()
    msg.get_bot.return_value = tg
    tg.delete_message = AsyncMock()
    tg.edit_message_text = AsyncMock()
    return msg


def test_seat_reply_rewrites_and_counts(seat):
    bot_mod, bot, logs = seat
    sent = []
    asyncio.run(bot._reply_smart(_message(sent), "워밍업 2세트 하고 들어갈게요."))
    body = "\n".join(sent)
    assert "웜업 2세트" in body and "워밍업" not in body
    assert any("stage=rewritten" in ln for ln in _leaks(logs))


def test_seat_reply_unlisted_prose_untouched_uncounted(seat):
    bot_mod, bot, logs = seat
    sent = []
    asyncio.run(bot._reply_smart(_message(sent), "좋아요, 첫 세트 들어갑니다."))
    assert "좋아요, 첫 세트 들어갑니다." in "\n".join(sent)
    assert _leaks(logs) == []


def test_seat_streamed_draft_is_force_edited_on_rewrite(seat):
    bot_mod, bot, logs = seat
    sent = []
    msg = _message(sent)
    asyncio.run(bot._reply_smart(msg, "워밍업 끝났어요.", streamed=True,
                                 draft_message_ids=[7]))
    tg = msg.get_bot.return_value
    assert tg.edit_message_text.await_count == 1
    edited = tg.edit_message_text.await_args
    text = edited.kwargs.get("text") or edited.args[0]
    assert "웜업 끝났어요." in text


def test_seat_send_smart_consumer_rail_untouched(seat):
    bot_mod, bot, logs = seat
    bot._send_text_body_chat = AsyncMock(return_value=[])
    asyncio.run(bot._send_smart(42, "워밍업 끝났어요.",
                                rail=bot_mod.RAIL_CONSUMER))
    assert "워밍업" in bot._send_text_body_chat.await_args.args[1]
    assert _leaks(logs) == []


def test_seat_send_smart_model_rail_rewritten(seat):
    bot_mod, bot, logs = seat
    bot._send_text_body_chat = AsyncMock(return_value=[])
    asyncio.run(bot._send_smart(42, "워밍업 끝났어요.", rail=bot_mod.RAIL_MODEL))
    assert "웜업 끝났어요." in bot._send_text_body_chat.await_args.args[1]
