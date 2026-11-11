"""First contact after /claim (rehearsal 2026-10-02).

A successful '/claim <code>' used to answer with a fixed system line
("이 봇의 소유자가 되셨습니다."). The owner asked that the first bubble be
the agent introducing itself instead, so the claim now opens the agent's own
first turn: bootstrap the owner stream, inject FIRST_CONTACT_TURN once.

Drives the REAL bot._handle_claim_attempt + ownership.verify_and_claim on a
throwaway bot_data_dir; only the SDK seams (ensure_owner_stream /
inject_background_turn) and the session store are faked.
  (a) right code -> exactly one stream bootstrap + one injection, carrying
      the first-contact turn, to the claimer's chat; no fixed reply line
  (b) the same claim again (code consumed) -> nothing more is triggered
  (c) wrong code -> nothing triggered, no reply (born-locked silence)
  (d) stream cannot start -> the CLAIM_SUCCESS fallback line, once
  (e) owner already spoke (inject refused on a live stream) -> no fallback
The hermetic PROJECT_ROOT (conftest) has no instance files, so (a)-(e)
stay on the model-turn path. The DGN-1849 machine-sent opener (an instance
with the onboarding verb) is test_dgn1849_machine_opener.py.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.bot as bot_mod
from bridge import messages, ownership
from bridge.bot import FIRST_CONTACT_TURN, TelegramBot, first_contact_turn

OWNER = 4242


def _bot():
    return object.__new__(TelegramBot)


def _update(text, user_id=OWNER):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(
        message=msg,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
    )


@pytest.fixture
def seams(tmp_path):
    data_dir = tmp_path / ".telegram_bot"
    data_dir.mkdir()
    code = ownership.ensure_claim_code(data_dir)
    sdk = SimpleNamespace(
        ensure_owner_stream=AsyncMock(return_value=True),
        inject_background_turn=AsyncMock(return_value=True),
    )
    sessions = SimpleNamespace(get_session=AsyncMock(return_value={"model": None}))
    with patch.object(bot_mod.config, "bot_data_dir", data_dir), \
         patch.object(bot_mod, "sdk_bridge", sdk), \
         patch.object(bot_mod, "session_manager", sessions):
        yield SimpleNamespace(code=code, sdk=sdk, data_dir=data_dir)


async def _claim(bot, update):
    allowed = await bot._handle_claim_attempt(update, update.effective_user.id)
    tasks = list(bot.__dict__.get("_first_contact_tasks", ()))
    if tasks:
        await asyncio.gather(*tasks)
    return allowed


def test_claim_opens_agent_first_turn_once(seams):
    bot = _bot()
    up = _update("/claim " + seams.code)
    assert asyncio.run(_claim(bot, up)) is False  # never reaches the model path
    assert (seams.data_dir / "owner.lock").read_text() == str(OWNER)
    seams.sdk.ensure_owner_stream.assert_awaited_once()
    args = seams.sdk.ensure_owner_stream.await_args.args
    assert args[0] == OWNER and args[2] == OWNER  # user id, chat id
    seams.sdk.inject_background_turn.assert_awaited_once_with(OWNER, first_contact_turn())
    up.message.reply_text.assert_not_awaited()  # no fixed ownership line

    # (b) replay of the same claim: the code is consumed -> nothing more.
    again = _update("/claim " + seams.code)
    asyncio.run(_claim(bot, again))
    assert seams.sdk.inject_background_turn.await_count == 1
    assert seams.sdk.ensure_owner_stream.await_count == 1
    again.message.reply_text.assert_not_awaited()


def test_wrong_code_triggers_nothing(seams):
    up = _update("/claim WRONGCODE", user_id=7)
    asyncio.run(_claim(_bot(), up))
    seams.sdk.ensure_owner_stream.assert_not_awaited()
    seams.sdk.inject_background_turn.assert_not_awaited()
    up.message.reply_text.assert_not_awaited()
    assert not (seams.data_dir / "owner.lock").exists()


def test_stream_failure_falls_back_to_one_line(seams):
    seams.sdk.ensure_owner_stream.return_value = False
    up = _update("/claim " + seams.code)
    asyncio.run(_claim(_bot(), up))
    seams.sdk.inject_background_turn.assert_not_awaited()
    up.message.reply_text.assert_awaited_once_with(messages.CLAIM_SUCCESS)


def test_inject_error_falls_back_to_one_line(seams):
    seams.sdk.inject_background_turn.side_effect = RuntimeError("cli died")
    up = _update("/claim " + seams.code)
    asyncio.run(_claim(_bot(), up))
    up.message.reply_text.assert_awaited_once_with(messages.CLAIM_SUCCESS)


def test_owner_spoke_first_no_fallback(seams):
    # A live stream refusing the injection means a real owner turn is in
    # flight: that turn opens the conversation, so nothing else is sent.
    seams.sdk.inject_background_turn.return_value = False
    up = _update("/claim " + seams.code)
    asyncio.run(_claim(_bot(), up))
    seams.sdk.inject_background_turn.assert_awaited_once()
    up.message.reply_text.assert_not_awaited()


def test_first_contact_turn_is_rules_only():
    # Owner rule: no hard-coded utterance -- the turn defers WHAT to say to
    # the agent's first-contact rules and carries no quoted sample line.
    assert FIRST_CONTACT_TURN.startswith("[bridge:first-contact]")
    assert '"' not in FIRST_CONTACT_TURN and "'" not in FIRST_CONTACT_TURN
    assert "first-contact rules" in FIRST_CONTACT_TURN


def test_first_contact_turn_restates_the_gate_rules():
    # DGN-1842: the turn names what the opener gate blocks, so the first
    # draft passes and no blocked draft reaches the owner.
    text = FIRST_CONTACT_TURN
    assert text.isascii()
    for rule in ("at most two short sentences", "no second-person pronoun",
                 "no name, title or address guessed", "asks no question",
                 "you are a personal agent, newly born",
                 "names no owner at all", "no second-person possessive",
                 "no substitute possessive or place word"):
        assert rule in text


def test_first_contact_turn_names_no_owner():
    # DGN-1848: a possessive opener identity ("personal agent of the user")
    # plus the pronoun ban made the model invent a substitute possessive.
    text = FIRST_CONTACT_TURN
    for bad in ("personal agent of the user", "user's personal agent",
                "your personal agent"):
        assert bad not in text


# ESTATE:BEGIN reads the estate onboarding gate's term list (routines/lib), which the public bridge does not ship; its first_contact_turn is the bare turn
def test_first_contact_turn_reuses_the_canonical_pronoun_list():
    import importlib.util
    from pathlib import Path
    import bridge.bot as bot_mod
    template = Path(bot_mod.__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "fc_terms_check", template / "routines" / "lib" / "first_contact_terms.py")
    terms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(terms)
    with patch.object(bot_mod, "PROJECT_ROOT", template):
        text = first_contact_turn()
    assert text.startswith(FIRST_CONTACT_TURN)
    for form in terms.KO_SECOND_PERSON + terms.KO_SUBSTITUTE_POSSESSIVE:
        assert form in text
    assert '"' not in text and "'" not in text
    # Missing term list: the bare turn, never an error.
    with patch.object(bot_mod, "PROJECT_ROOT", Path("/nonexistent-dgn1842")):
        assert first_contact_turn() == FIRST_CONTACT_TURN
# ESTATE:END
