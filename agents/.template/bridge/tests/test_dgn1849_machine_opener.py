"""DGN-1849 (dec-243): the machine-sent first-contact opener.

On a kit-pending main with onboarding pending the bridge sends the fixed
opener itself (i18n onboarding.opener) -- no stream, no model turn -- and
records it on the first-contact state (source bridge):
  (f) ko and en: the opener text equals the i18n copy exactly, sent once
  (g) a domain-role instance keeps the model-composed opener (injection)
  (h) a failed fixed send falls back to the model-turn path

Split from test_first_contact_claim.py: these cases need a real instance
(the template's routines/ onboarding verb + its imports, the persona
template, config/i18n), which the public bridge does not ship, so this
suite is manifest-excluded from the extraction (UPSTREAM.md). The bridge
side stays fail-open there; test_first_contact_claim.py covers that path.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import bridge.bot as bot_mod
from bridge.tests.test_first_contact_claim import (  # noqa: F401 -- seams is a fixture
    _bot, _claim, _update, seams,
)

TEMPLATE = Path(bot_mod.__file__).resolve().parents[1]


def _instance(tmp_path, lang="ko", role=None):
    root = tmp_path / "inst"
    (root / "identity").mkdir(parents=True)
    (root / "config").mkdir()
    os.symlink(TEMPLATE / "routines", root / "routines")
    persona = (TEMPLATE / "identity" / "hot.custom.agent.md").read_text()
    if role:
        persona = persona.replace(
            "- Primary focus: (set at onboarding -- one prose line naming the main hat;\n"
            "  the general front-door bullet above still applies)",
            "- Primary focus: " + role)
    (root / "identity" / "hot.custom.agent.md").write_text(persona)
    (root / "config" / "i18n").mkdir()
    for lg in ("ko", "en"):
        (root / "config" / "i18n" / (lg + ".json")).write_text(
            (TEMPLATE / "config" / "i18n" / (lg + ".json")).read_text())
    (root / "config" / "agent.conf").write_text(
        "AGENT_LANG=%s\nKIT=%s\n" % (lang, "none" if role else "pending"))
    return root


def _copy(lang, key):
    return json.loads((TEMPLATE / "config" / "i18n" / (lang + ".json"))
                      .read_text(encoding="utf-8"))[key]


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_kit_pending_main_gets_the_fixed_opener(seams, tmp_path, lang):
    root = _instance(tmp_path, lang)
    with patch.object(bot_mod, "PROJECT_ROOT", root):
        up = _update("/claim " + seams.code)
        asyncio.run(_claim(_bot(), up))
    up.message.reply_text.assert_awaited_once_with(_copy(lang, "onboarding.opener"))
    seams.sdk.ensure_owner_stream.assert_not_awaited()   # no model turn at all
    seams.sdk.inject_background_turn.assert_not_awaited()
    rec = json.loads((root / ".telegram_bot" / "logs" / ".onboarding-opener-sent")
                     .read_text(encoding="utf-8"))
    assert rec["source"] == "bridge"
    assert rec["text"] == _copy(lang, "onboarding.opener")
    assert rec["q1_due"] is True


def test_domain_role_keeps_model_opener(seams, tmp_path):
    root = _instance(tmp_path, role="health coach -- exercise and diet")
    with patch.object(bot_mod, "PROJECT_ROOT", root):
        up = _update("/claim " + seams.code)
        asyncio.run(_claim(_bot(), up))
    seams.sdk.inject_background_turn.assert_awaited_once()
    up.message.reply_text.assert_not_awaited()
    assert not (root / ".telegram_bot" / "logs" / ".onboarding-opener-sent").exists()


def test_fixed_opener_send_failure_falls_back_to_model_turn(seams, tmp_path):
    root = _instance(tmp_path)
    with patch.object(bot_mod, "PROJECT_ROOT", root):
        up = _update("/claim " + seams.code)
        up.message.reply_text.side_effect = RuntimeError("telegram down")
        asyncio.run(_claim(_bot(), up))
    seams.sdk.inject_background_turn.assert_awaited_once()
    assert not (root / ".telegram_bot" / "logs" / ".onboarding-opener-sent").exists()
