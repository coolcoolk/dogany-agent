"""DGN-1914: local STT installs on demand from the first voice message."""
import asyncio
import os
from types import SimpleNamespace

import pytest

from bridge import bot as bot_mod
from bridge import messages, voice_setup

_REAL_NEEDS_INSTALL = voice_setup.needs_install


# --- voice_setup ----------------------------------------------------------


@pytest.mark.parametrize("ram, disk, expected", [
    (None, 100, "small"), (4, 100, "small"), (8, 100, "small"),
    (12, 100, "small"), (16, 5, "small"), (16, 6, "large-v3-turbo"),
    (31, 100, "large-v3-turbo"), (32, 13, "large-v3-turbo"), (64, None, "small"),
])
def test_recommend_model_turbo_when_comfortable(ram, disk, expected):
    assert voice_setup.recommend_model(ram, disk) == expected


def test_needs_install_gates_only_unconfigured_uncached(monkeypatch):
    monkeypatch.setattr(voice_setup, "package_available", lambda: True)
    monkeypatch.setattr(voice_setup, "model_cached", lambda name: False)
    monkeypatch.delenv("LOCAL_WHISPER_MODEL", raising=False)
    assert voice_setup.needs_install("small") is True
    # Existing installs that chose a model are untouched.
    monkeypatch.setenv("LOCAL_WHISPER_MODEL", "medium")
    assert voice_setup.needs_install("small") is False
    monkeypatch.delenv("LOCAL_WHISPER_MODEL")
    # Already cached (e.g. the old hidden first-use download): no prompt.
    monkeypatch.setattr(voice_setup, "model_cached", lambda name: True)
    assert voice_setup.needs_install("small") is False
    # Package missing: the existing VOICE_UNAVAILABLE path reports it.
    monkeypatch.setattr(voice_setup, "model_cached", lambda name: False)
    monkeypatch.setattr(voice_setup, "package_available", lambda: False)
    assert voice_setup.needs_install("small") is False


def test_persist_model_replaces_or_appends_and_keeps_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCAL_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(voice_setup.config, "local_whisper_model", "small")
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=TEST-ONLY-x\nLOCAL_WHISPER_MODEL=small\nLOCALE=ko\n")
    os.chmod(env, 0o600)
    voice_setup.persist_model("medium", env)
    assert env.read_text() == "TELEGRAM_BOT_TOKEN=TEST-ONLY-x\nLOCAL_WHISPER_MODEL=medium\nLOCALE=ko\n"
    assert env.stat().st_mode & 0o777 == 0o600
    assert os.environ["LOCAL_WHISPER_MODEL"] == "medium"
    assert voice_setup.config.local_whisper_model == "medium"
    env.write_text("TELEGRAM_BOT_TOKEN=TEST-ONLY-x")
    voice_setup.persist_model("small", env)
    assert env.read_text() == "TELEGRAM_BOT_TOKEN=TEST-ONLY-x\nLOCAL_WHISPER_MODEL=small\n"
    assert not list(tmp_path.glob("*.voice-tmp"))


# --- bot glue -------------------------------------------------------------


class _Msg:
    def __init__(self):
        self.voice = SimpleNamespace(duration=3, file_id="f", mime_type="audio/ogg")
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs.get("reply_markup")))


def _update(uid=7):
    return SimpleNamespace(message=_Msg(), effective_user=SimpleNamespace(id=uid),
                           effective_chat=SimpleNamespace(id=uid))


class _Query:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


def _bot(monkeypatch, needs):
    b = bot_mod.TelegramBot()
    transcribed = []

    async def allow(update, **kwargs):
        return True

    async def fake_transcribe(update):
        transcribed.append(update)

    monkeypatch.setattr(b, "_check_access", allow)
    monkeypatch.setattr(b, "_transcribe_voice", fake_transcribe)
    monkeypatch.setattr(voice_setup, "needs_install", lambda name=None: needs[0])
    monkeypatch.setattr(voice_setup, "pick_model", lambda: "small")
    # Single tier by default (the one-button flow); picker tests override.
    monkeypatch.setattr(voice_setup, "current_tier_options",
                        lambda: voice_setup.tier_options(None, None))
    return b, transcribed


def _button_data(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


@pytest.mark.asyncio
async def test_voice_without_stt_is_held_and_offers_one_button(monkeypatch):
    b, transcribed = _bot(monkeypatch, [True])
    upd = _update()
    await b._handle_voice_message(upd, None)
    assert transcribed == []
    assert b._pending_voice == {7: [upd]}
    [(text, markup)] = upd.message.replies
    assert text == voice_setup.install_prompt("small")
    assert _button_data(markup) == [voice_setup.INSTALL_CALLBACK]


@pytest.mark.asyncio
async def test_configured_voice_goes_straight_to_transcription(monkeypatch):
    b, transcribed = _bot(monkeypatch, [False])
    upd = _update()
    await b._handle_voice_message(upd, None)
    assert transcribed == [upd]
    assert upd.message.replies == []


@pytest.mark.asyncio
async def test_tap_installs_then_transcribes_held_messages(monkeypatch):
    needs = [True]
    b, transcribed = _bot(monkeypatch, needs)
    gate = asyncio.Event()
    installed = []

    def fake_install(name):
        installed.append(name)

    monkeypatch.setattr(voice_setup, "install_model", fake_install)
    first, second = _update(), _update()
    await b._handle_voice_message(first, None)
    query = _Query()

    real_run = b._run_voice_install

    async def gated_run(q, model, *rest):
        await gate.wait()
        await real_run(q, model, *rest)

    monkeypatch.setattr(b, "_run_voice_install", gated_run)
    await b._handle_voice_install_tap(query)
    # A voice message during the install waits, no second button.
    await b._handle_voice_message(second, None)
    assert second.message.replies == [(messages.VOICE_INSTALL_WAIT, None)]
    await b._handle_voice_install_tap(query)  # double tap is a no-op
    gate.set()
    await b._voice_install_task
    assert installed == ["small"]
    assert transcribed == [first, second]
    assert b._pending_voice == {}
    assert b._voice_install_task is None
    assert query.edits[0][0] == messages.VOICE_INSTALLING.format(model="small", size="0.5GB")
    assert query.edits[-1][0] == messages.VOICE_INSTALL_DONE


@pytest.mark.asyncio
async def test_failed_install_keeps_messages_and_offers_retry(monkeypatch):
    b, transcribed = _bot(monkeypatch, [True])

    def boom(name):
        raise OSError("offline")

    monkeypatch.setattr(voice_setup, "install_model", boom)
    upd = _update()
    await b._handle_voice_message(upd, None)
    query = _Query()
    await b._handle_voice_install_tap(query)
    await b._voice_install_task
    assert transcribed == []
    assert b._pending_voice == {7: [upd]}
    assert b._voice_install_task is None
    text, markup = query.edits[-1]
    assert text == messages.VOICE_INSTALL_FAILED
    assert _button_data(markup) == [voice_setup.INSTALL_CALLBACK]


# --- follow-up: the owner picks a tier -------------------------------------

from bridge.i18n import en as en_catalog, ko as ko_catalog  # noqa: E402

KO = ko_catalog.STRINGS.__getitem__
EN = en_catalog.STRINGS.__getitem__
S, T = "small", "large-v3-turbo"


def _summary(opts):
    return [(o.model, o.slow, o.recommended) for o in opts]


@pytest.mark.parametrize("ram, disk, expected", [
    # unknown device -> small only
    (None, 100, [(S, False, True)]),
    (16, None, [(S, False, True)]),
    (4, 100, [(S, False, True)]),
    (7, 100, [(S, False, True)]),
    # 8-15GB: turbo shown but slow, small recommended
    (8, 100, [(S, False, True), (T, True, False)]),
    (12, 100, [(S, False, True), (T, True, False)]),
    # disk below download + headroom -> small only
    (8, 3, [(S, False, True)]),
    (64, 2, [(S, False, True)]),
    # 16GB+: turbo recommended
    (16, 100, [(S, False, False), (T, False, True)]),
    (16, 6, [(S, False, False), (T, False, True)]),
    (32, 100, [(S, False, False), (T, False, True)]),
    # 16GB, disk tight (shown, not comfortable): small recommended
    (16, 5, [(S, False, True), (T, False, False)]),
    (16, 4, [(S, False, True), (T, False, False)]),
])
def test_tier_visibility_and_recommendation(ram, disk, expected):
    opts = voice_setup.tier_options(ram, disk)
    assert _summary(opts) == expected
    assert [o.model for o in opts if o.recommended] == [voice_setup.recommend_model(ram, disk)]


KO_HEAD = "음성 메시지를 알아들으려면 음성 인식을 먼저 설치해야 해요. 어떤 걸로 할까요?"
KO_FOOT = "고른 뒤에도 \"음성 인식 바꿔줘\"라고 하시면 다시 고를 수 있어요."
KO_SMALL = "· 빠르게 (OpenAI Whisper small): 가볍고 빨라요. 한국어는 가끔 틀려요. (약 0.5GB)"


def _ko(ram, disk=100, voice="ko"):
    return voice_setup.render_picker(voice_setup.tier_options(ram, disk), KO, "ko", voice)


def test_picker_text_ko_16gb_exact():
    assert _ko(16) == "\n".join([
        KO_HEAD,
        KO_SMALL,
        "· 정확하게 (OpenAI Whisper large-v3-turbo): 한국어를 훨씬 잘 알아들어요. (약 1.6GB) (추천)",
        KO_FOOT,
    ])


def test_picker_text_ko_12gb_exact_slow_and_small_recommended():
    assert _ko(12) == "\n".join([
        KO_HEAD,
        KO_SMALL + " (추천)",
        "· 정확하게 (OpenAI Whisper large-v3-turbo): 한국어를 훨씬 잘 알아들어요. 이 맥에서는 느려요. (약 1.6GB)",
        KO_FOOT,
    ])


def test_picker_text_en_16gb_english_specific():
    text = voice_setup.render_picker(voice_setup.tier_options(16, 100), EN, "en", "en")
    assert text.splitlines()[1:3] == [
        "· Fast (OpenAI Whisper small): Light and fast. Good for clear English. (about 0.5GB)",
        "· Accurate (OpenAI Whisper large-v3-turbo): Catches English more reliably, "
        "even with accents or noise. (about 1.6GB) (recommended)",
    ]


def test_picker_en_slow_mark():
    text = voice_setup.render_picker(voice_setup.tier_options(12, 100), EN, "en", "en")
    assert text.splitlines()[2] == (
        "· Accurate (OpenAI Whisper large-v3-turbo): Catches English more reliably, "
        "even with accents or noise. Slow on this Mac. (about 1.6GB)")


def test_picker_generic_desc_when_voice_language_differs(monkeypatch):
    text = _ko(16, voice="ja")
    assert text.splitlines()[1:3] == [
        "· 빠르게 (OpenAI Whisper small): 가볍고 빨라요. 가끔 틀려요. (약 0.5GB)",
        "· 정확하게 (OpenAI Whisper large-v3-turbo): 훨씬 정확하게 알아들어요. (약 1.6GB) (추천)",
    ]
    en = voice_setup.render_picker(voice_setup.tier_options(16, 100), EN, "en", "ko")
    assert en.splitlines()[1:3] == [
        "· Fast (OpenAI Whisper small): Light and fast. Makes more mistakes. (about 0.5GB)",
        "· Accurate (OpenAI Whisper large-v3-turbo): Much more accurate. (about 1.6GB) (recommended)",
    ]
    # voice language "auto" (config resolves it to None) never claims a
    # specific language; voice_lang=None here means "read config"
    monkeypatch.setattr(voice_setup.config, "whisper_language", None)
    assert _ko(16, voice=None) == text


@pytest.mark.parametrize("catalog, voice, expected", [
    ("ko", "ko", True), ("en", "en", True), ("ko", "ko-KR", True), ("en", "EN_us", True),
    ("ko", "ja", False), ("en", "ko", False), ("ko", None, False), ("ko", "", False),
])
def test_language_matches(catalog, voice, expected):
    assert voice_setup.language_matches(catalog, voice) is expected


def test_picker_defaults_follow_config(monkeypatch):
    monkeypatch.setattr(voice_setup.config, "whisper_language", None)
    opts = voice_setup.tier_options(16, 100)
    assert voice_setup.render_picker(opts, KO, "ko") == _ko(16, voice="ja")


def test_tier_labels():
    assert [voice_setup.tier_label(m, KO) for m in voice_setup.TIERS] == ["빠르게", "정확하게"]
    assert [voice_setup.tier_label(m, EN) for m in voice_setup.TIERS] == ["Fast", "Accurate"]


def test_change_request_phrase():
    for text in ("음성 인식 바꿔줘", "음성인식 바꿔줘!", " 음성 인식 바꿔 줘 "):
        assert voice_setup.is_change_request(text, KO)
    for text in ("음성 인식 바꿔줘 그리고 일정 알려줘", "안녕", ""):
        assert not voice_setup.is_change_request(text, KO)
    assert voice_setup.is_change_request("Change voice recognition.", EN)


def _with_tiers(monkeypatch, ram, disk):
    monkeypatch.setattr(voice_setup, "current_tier_options",
                        lambda: voice_setup.tier_options(ram, disk))


@pytest.mark.asyncio
@pytest.mark.parametrize("ram, buttons", [
    (8, ["vstt:pick:small", "vstt:pick:large-v3-turbo"]),
    (32, ["vstt:pick:small", "vstt:pick:large-v3-turbo"]),
])
async def test_voice_offer_shows_tier_buttons(monkeypatch, ram, buttons):
    b, _ = _bot(monkeypatch, [True])
    _with_tiers(monkeypatch, ram, 100)
    upd = _update()
    await b._handle_voice_message(upd, None)
    [(text, markup)] = upd.message.replies
    opts = voice_setup.tier_options(ram, 100)
    assert text == voice_setup.render_picker(opts)
    assert _button_data(markup) == buttons
    assert [btn.text for row in markup.inline_keyboard for btn in row] == [
        voice_setup.tier_label(o.model) for o in opts]


@pytest.mark.asyncio
@pytest.mark.parametrize("ram, disk", [(4, 100), (None, 100), (64, None)])
async def test_single_tier_keeps_one_button_flow(monkeypatch, ram, disk):
    b, _ = _bot(monkeypatch, [True])
    _with_tiers(monkeypatch, ram, disk)
    upd = _update()
    await b._handle_voice_message(upd, None)
    [(text, markup)] = upd.message.replies
    assert text == voice_setup.install_prompt("small")
    assert _button_data(markup) == [voice_setup.INSTALL_CALLBACK]


@pytest.mark.asyncio
async def test_tier_tap_installs_persists_and_transcribes(monkeypatch, tmp_path):
    b, transcribed = _bot(monkeypatch, [True])
    _with_tiers(monkeypatch, 16, 100)
    env = tmp_path / ".env"
    env.write_text("LOCALE=ko\n")
    downloaded = []
    fw_utils = pytest.importorskip("faster_whisper.utils")
    monkeypatch.setattr(fw_utils, "download_model", lambda name, **kw: downloaded.append(name))
    monkeypatch.setattr(voice_setup, "PROJECT_ENV_PATH", env)
    monkeypatch.delenv("LOCAL_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(voice_setup.config, "local_whisper_model", "small")
    upd = _update()
    await b._handle_voice_message(upd, None)
    query = _Query()
    await b._handle_voice_install_tap(query, "vstt:pick:large-v3-turbo")
    await b._voice_install_task
    assert downloaded == ["large-v3-turbo"]
    assert env.read_text() == "LOCALE=ko\nLOCAL_WHISPER_MODEL=large-v3-turbo\n"
    assert os.environ["LOCAL_WHISPER_MODEL"] == "large-v3-turbo"
    assert transcribed == [upd]
    assert query.edits[0][0] == messages.VOICE_INSTALLING.format(model="large-v3-turbo", size="1.6GB")
    assert query.edits[-1][0] == messages.VOICE_INSTALL_DONE


@pytest.mark.asyncio
async def test_tier_tap_failure_retries_same_tier_and_bogus_tier_ignored(monkeypatch):
    b, transcribed = _bot(monkeypatch, [True])

    def boom(name):
        raise OSError("offline")

    monkeypatch.setattr(voice_setup, "install_model", boom)
    query = _Query()
    await b._handle_voice_install_tap(query, "vstt:pick:large-v3-turbo")
    await b._voice_install_task
    assert _button_data(query.edits[-1][1]) == ["vstt:pick:large-v3-turbo"]
    bogus = _Query()
    await b._handle_voice_install_tap(bogus, "vstt:pick:huge")
    assert bogus.edits == [] and b._voice_install_task is None


@pytest.mark.asyncio
async def test_change_phrase_reopens_picker(monkeypatch):
    b, _ = _bot(monkeypatch, [False])
    _with_tiers(monkeypatch, 32, 100)
    monkeypatch.setattr(voice_setup, "package_available", lambda: True)
    monkeypatch.setattr(voice_setup, "is_change_request", lambda text: text == "CHANGE")
    msg = _Msg()
    assert await b._maybe_offer_voice_change(msg, "CHANGE") is True
    [(text, markup)] = msg.replies
    assert text == voice_setup.render_picker(voice_setup.tier_options(32, 100))
    assert len(_button_data(markup)) == 2
    assert await b._maybe_offer_voice_change(_Msg(), "hello") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("old", ["medium", "large-v3"])
async def test_retired_tiers_not_pickable_but_existing_installs_ungated(monkeypatch, old):
    b, _ = _bot(monkeypatch, [True])
    query = _Query()
    await b._handle_voice_install_tap(query, "vstt:pick:" + old)
    assert query.edits == [] and b._voice_install_task is None
    monkeypatch.setattr(voice_setup, "package_available", lambda: True)
    monkeypatch.setattr(voice_setup, "model_cached", lambda name: False)
    monkeypatch.setenv("LOCAL_WHISPER_MODEL", old)
    assert _REAL_NEEDS_INSTALL() is False
    assert old in voice_setup.MODEL_SIZES


def test_install_prompt_names_the_model_and_its_size():
    """dec-321: the one-button offer names what the tap installs, with the
    size from MODEL_SIZES (never a hardcoded figure in the copy)."""
    for cat in (en_catalog, ko_catalog):
        assert "{name}" in cat.STRINGS["voice_install_prompt"]
        assert "{size}" in cat.STRINGS["voice_install_prompt"]
        assert "0.5GB" not in cat.STRINGS["voice_install_prompt"]
    ko = lambda key: ko_catalog.STRINGS[key]
    assert voice_setup.install_prompt("small", ko) == (
        "음성 메시지를 알아들으려면 음성 인식을 먼저 설치해야 해요. "
        "아래 버튼을 누르면 OpenAI Whisper small(약 %s)을 설치하고 "
        "이 음성도 바로 읽어드릴게요." % voice_setup.MODEL_SIZES["small"]
    )
    en = lambda key: en_catalog.STRINGS[key]
    text = voice_setup.install_prompt("large-v3-turbo", en)
    assert "OpenAI Whisper large-v3-turbo (about %s)" % voice_setup.MODEL_SIZES["large-v3-turbo"] in text
    assert voice_setup.install_prompt("medium", en).count("medium (about 1.5GB)") == 1
