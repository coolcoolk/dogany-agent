"""Retire old intro keyboards using the bridge's existing answer latch."""
import importlib.util
import json
from pathlib import Path
import sys

from conf_reader import conf_get


def load(root, name):
    spec = importlib.util.spec_from_file_location(name, root / "bridge" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def retire(root):
    bot = root / ".telegram_bot"
    try:
        state = json.loads((bot / "retro-slot.json").read_text())
    except FileNotFoundError:
        return
    intro = state.get("intro") or {}
    mid, chat = intro.get("message_id"), intro.get("chat_id")
    if not isinstance(mid, int) or isinstance(mid, bool):
        return
    if chat is None:
        # Onboarding recorded only the message id. Resolve its private-chat
        # recipient through the same configuration/owner API as push.sh.
        allowed = conf_get("ALLOWED_USER_IDS", str(bot / ".env"))
        if allowed:
            chat = int(allowed.split(",")[0])
        elif (root / "bridge/ownership.py").is_file():
            chat = load(root, "ownership").read_owner_lock(bot)
    if not isinstance(chat, int) or isinstance(chat, bool):
        return  # An unclaimed install has no owner keyboard to retire.
    menus = load(root, "option_menus").OptionMenus(bot / "option-menu-answers.json")
    menus.answer(chat, mid, "retired-intro")


if __name__ == "__main__":
    retire(Path(sys.argv[1]))
