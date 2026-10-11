"""DGN-919: slash-command menu polish -- single-source drift-proof invariant.

Carried to canonical with DGN-986 [c]: this test previously lived only in the
dev instance (the DGN-919 carry commit brought bot.py/messages/i18n but not
the test). It lands here together with the D1 amendment so the menu lock is
enforced where the menu is defined.

Locked list history:
  - 2026-08-17: owner locked the 10-command menu (DGN-919).
  - 2026-08-21: owner DIRECTLY amended the lock (DGN-986 D1): /health is
    inserted after authsync, before help -> 11 commands.
  - DGN-986 integration merge (main catch-up): two independent main-side
    changes landed on the D1 anchor point AFTER D1 was decided --
    DGN-1050 retired /authsync from this menu entirely (security fix, the
    file->keychain overwrite bug), and DGN-997 added /restart just before
    help. D1's literal anchor ("after authsync") no longer exists, so this
    merge keeps the still-satisfiable half of D1 ("help 앞"/before help)
    and does not touch the DGN-997/DGN-1050 ordering as landed on main:
    ... resume, restart, health, help (still 11 commands: authsync's slot
    is replaced by restart's, one-for-one). Flagged for owner
    reconfirmation -- not a re-run of D1 itself.
  - 2026-09-12: owner pulled /health off the command surface entirely
    (DGN-1435 -- the "확인 필요" report was unreliable: retired agents and
    one-shot reminders counted as loss, raw English machine keys and plist
    paths leaked into user-facing text). bot.py no longer registers /health
    or lists it in COMMAND_MENU_SPEC -- back to the 10-command DGN-919 lock
    until a rewrite re-adds it. The DGN-986 D1 history above is kept for the
    rewrite to consult, not as a currently-true count.

Asserts that:
  1. COMMAND_MENU_SPEC matches the explicit ordered list (12 canonical,
     11 public after stripping the DGN-1417 estate-only /update entry).
  2. _set_bot_commands builds its BotCommand list from COMMAND_MENU_SPEC (same order).
  3. The generated /help body lists the same commands in the same order.
  4. Hidden commands (start, claim, usageretry) are NOT in COMMAND_MENU_SPEC.
  5. kill is NOT in COMMAND_MENU_SPEC (no handler; must never appear).
  6. All cmd_desc_* i18n keys exist in both ko and en catalogs and are non-empty.
  7. Menu order == help-list order == spec order (the drift-proof invariant).
"""

import asyncio
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bridge.bot import COMMAND_MENU_SPEC, TelegramBot
from bridge import messages
from bridge.i18n import en, ko

REPO_ROOT = Path(__file__).resolve().parents[4]
# The region lint lives in the canonical repo's tests/, outside the bridge
# tree: the OSS artifact and the extract gate-3 harness do not ship it, so the
# OSS-stripped-order check runs only where it exists (DGN-1779).
_REGION_LINT = REPO_ROOT / "tests" / "dgn818_ownership_lint.py"


# ---------------------------------------------------------------------------
# Locked spec: the authoritative ordered list (DGN-1771 owner reorder
# 2026-09-28: new/stop/usage/update/model first, rest keep relative order).
# ---------------------------------------------------------------------------
EXPECTED_SPEC = [
    "new",
    "stop",
    "usage",
    # ESTATE:BEGIN DGN-1417 adds /update before /model in the canonical menu
    "update",
    # ESTATE:END
    "model",
    "btw",
    "queue",
    "skills",
    "resume",
    "restart",
    # DGN-1112: /login joins the menu after the owner approved its copy
    # (dec-266, 2026-10-06); slotted after restart, help stays last.
    "login",
    # health pulled off the command surface (DGN-1435, 2026-09-12) -- not
    # carried here until the rewrite re-adds it; see module docstring.
    "help",
]

# The OSS mirror strips the DGN-1417 estate region, so /update never
# ships there (DGN-1771 note: strip must still yield new, stop, usage, model,
# btw, queue, skills, resume, restart, login, help).
EXPECTED_SPEC_OSS_STRIPPED = [c for c in EXPECTED_SPEC if c != "update"]

# authsync retired from the menu by DGN-1050 (still registered off-menu, see
# test_dgn759_authsync_command.py's test_authsync_not_in_command_menu_spec).
HIDDEN_COMMANDS = {"start", "claim", "usageretry", "authsync"}
FORBIDDEN_COMMANDS = {"kill"}


class TestCommandMenuSpec(unittest.TestCase):
    """COMMAND_MENU_SPEC structure and content."""

    def test_spec_has_exactly_expected_entries(self):
        # The expected list is independent of the production spec and shares
        # its extraction boundary, so both builds retain an exact count.
        self.assertEqual(len(COMMAND_MENU_SPEC), len(EXPECTED_SPEC))

    def test_spec_order_matches_locked_list(self):
        names = [cmd for cmd, _ in COMMAND_MENU_SPEC]
        self.assertEqual(names, EXPECTED_SPEC,
                         f"COMMAND_MENU_SPEC order mismatch.\n"
                         f"  got:      {names}\n"
                         f"  expected: {EXPECTED_SPEC}")

    # test_health_sits_immediately_before_help retired here (DGN-1435,
    # 2026-09-12): /health no longer sits in COMMAND_MENU_SPEC at all, in
    # either build. help-is-last stays covered by
    # test_spec_order_matches_locked_list.

    def test_hidden_commands_not_in_spec(self):
        names = {cmd for cmd, _ in COMMAND_MENU_SPEC}
        for hidden in HIDDEN_COMMANDS:
            self.assertNotIn(hidden, names,
                             f"Hidden command /{hidden} must not appear in COMMAND_MENU_SPEC")

    def test_kill_not_in_spec(self):
        names = {cmd for cmd, _ in COMMAND_MENU_SPEC}
        for forbidden in FORBIDDEN_COMMANDS:
            self.assertNotIn(forbidden, names,
                             f"/{forbidden} has no handler and must not be in COMMAND_MENU_SPEC")

    def test_desc_callables_return_nonempty_strings(self):
        for cmd, desc_fn in COMMAND_MENU_SPEC:
            val = desc_fn()
            self.assertIsInstance(val, str, f"/{cmd} desc_fn() must return str")
            self.assertTrue(val.strip(), f"/{cmd} desc_fn() must return a non-empty string")


class TestBotCommandMenuOrder(unittest.TestCase):
    """_set_bot_commands produces a BotCommand list derived from COMMAND_MENU_SPEC."""

    def _run_set_bot_commands(self):
        app = MagicMock()
        app.bot = AsyncMock()
        app.bot.delete_my_commands = AsyncMock()
        app.bot.set_my_commands = AsyncMock()
        bot_instance = object.__new__(TelegramBot)
        bot_instance.application = app
        asyncio.run(bot_instance._set_bot_commands())
        set_calls = app.bot.set_my_commands.call_args_list
        self.assertTrue(set_calls, "set_my_commands was never called")
        return set_calls[0][0][0]  # first positional arg of first call

    def test_menu_order_matches_spec(self):
        commands = self._run_set_bot_commands()
        menu_names = [c.command for c in commands]
        spec_names = [cmd for cmd, _ in COMMAND_MENU_SPEC]
        self.assertEqual(menu_names, spec_names,
                         f"BotCommand menu order does not match COMMAND_MENU_SPEC.\n"
                         f"  menu: {menu_names}\n"
                         f"  spec: {spec_names}")

    def test_menu_has_exactly_expected_entries(self):
        commands = self._run_set_bot_commands()
        self.assertEqual(len(commands), len(EXPECTED_SPEC))

    def test_menu_descriptions_match_spec(self):
        commands = self._run_set_bot_commands()
        for (spec_cmd, desc_fn), bc in zip(COMMAND_MENU_SPEC, commands):
            self.assertEqual(bc.command, spec_cmd)
            self.assertEqual(bc.description, desc_fn(),
                             f"/{spec_cmd} BotCommand description mismatch")


class TestHelpTextOrder(unittest.TestCase):
    """_cmd_help generates a numbered list matching COMMAND_MENU_SPEC order."""

    def _generate_help_body(self):
        """Replicate the generation logic from _cmd_help."""
        lines = [messages.HELP_TEXT_HEADER]
        for i, (cmd, desc_fn) in enumerate(COMMAND_MENU_SPEC, start=1):
            lines.append(f"{i}. /{cmd} - {desc_fn()}")
        lines.append("")
        lines.append(messages.HELP_TEXT_FOOTER)
        return "\n".join(lines)

    def test_help_body_order_matches_spec(self):
        body = self._generate_help_body()
        spec_names = [cmd for cmd, _ in COMMAND_MENU_SPEC]
        import re
        found = re.findall(r"^\d+\. /(\w+)", body, re.MULTILINE)
        self.assertEqual(found, spec_names,
                         f"Help body command order mismatch.\n"
                         f"  found: {found}\n"
                         f"  spec:  {spec_names}")

    def test_help_body_contains_all_spec_commands(self):
        body = self._generate_help_body()
        for cmd, _ in COMMAND_MENU_SPEC:
            self.assertIn(f"/{cmd}", body, f"/{cmd} missing from generated help body")

    def test_help_body_contains_footer(self):
        body = self._generate_help_body()
        self.assertIn(messages.HELP_TEXT_FOOTER, body)

    def test_help_body_contains_header(self):
        body = self._generate_help_body()
        self.assertTrue(body.startswith(messages.HELP_TEXT_HEADER))


class TestDriftProofInvariant(unittest.TestCase):
    """Menu order == help-list order == spec order (the core DGN-919 guarantee)."""

    def test_menu_help_spec_all_match(self):
        """All three surfaces must agree on command order."""
        spec_names = [cmd for cmd, _ in COMMAND_MENU_SPEC]

        # Surface 1: BotCommand menu (via _set_bot_commands)
        app = MagicMock()
        app.bot = AsyncMock()
        app.bot.delete_my_commands = AsyncMock()
        app.bot.set_my_commands = AsyncMock()
        bot_instance = object.__new__(TelegramBot)
        bot_instance.application = app
        asyncio.run(bot_instance._set_bot_commands())
        menu_names = [c.command for c in app.bot.set_my_commands.call_args_list[0][0][0]]

        # Surface 2: /help body (generated in _cmd_help)
        import re
        lines = [messages.HELP_TEXT_HEADER]
        for i, (cmd, desc_fn) in enumerate(COMMAND_MENU_SPEC, start=1):
            lines.append(f"{i}. /{cmd} - {desc_fn()}")
        lines.append("")
        lines.append(messages.HELP_TEXT_FOOTER)
        body = "\n".join(lines)
        help_names = re.findall(r"^\d+\. /(\w+)", body, re.MULTILINE)

        self.assertEqual(spec_names, menu_names,
                         "SPEC vs MENU order mismatch")
        self.assertEqual(spec_names, help_names,
                         "SPEC vs HELP order mismatch")
        self.assertEqual(menu_names, help_names,
                         "MENU vs HELP order mismatch")


class TestI18nKeyParity(unittest.TestCase):
    """All cmd_desc_* keys exist in both ko and en catalogs and are non-empty."""

    def test_all_cmd_desc_keys_in_ko_and_en(self):
        spec_names = [cmd for cmd, _ in COMMAND_MENU_SPEC]
        for cmd in spec_names:
            key = f"cmd_desc_{cmd}"
            self.assertIn(key, ko.STRINGS, f"ko.STRINGS missing key: {key}")
            self.assertIn(key, en.STRINGS, f"en.STRINGS missing key: {key}")
            self.assertTrue(ko.STRINGS[key].strip(),
                            f"ko.STRINGS[{key!r}] is empty")
            self.assertTrue(en.STRINGS[key].strip(),
                            f"en.STRINGS[{key!r}] is empty")

    def test_help_text_header_in_both_locales(self):
        self.assertIn("help_text_header", ko.STRINGS)
        self.assertIn("help_text_header", en.STRINGS)

    def test_help_text_footer_in_both_locales(self):
        self.assertIn("help_text_footer", ko.STRINGS)
        self.assertIn("help_text_footer", en.STRINGS)

    def test_hidden_commands_have_no_cmd_desc_keys(self):
        # start, claim, usageretry are hidden -- no cmd_desc entry should exist.
        for hidden in HIDDEN_COMMANDS:
            key = f"cmd_desc_{hidden}"
            self.assertNotIn(key, ko.STRINGS,
                             f"ko.STRINGS should not have {key!r} (hidden command)")
            self.assertNotIn(key, en.STRINGS,
                             f"en.STRINGS should not have {key!r} (hidden command)")


@unittest.skipUnless(_REGION_LINT.exists(), "region lint not shipped here")
class TestOssStrippedOrder(unittest.TestCase):
    """The OSS mirror (ESTATE regions stripped) keeps the DGN-1771 order."""

    def test_stripped_command_menu_spec_order(self):
        sys.path.insert(0, str(_REGION_LINT.parent))
        from dgn818_ownership_lint import parse_regions, strip_regions
        bot_py = REPO_ROOT / "agents" / ".template" / "bridge" / "bot.py"
        lines = bot_py.read_text(encoding="utf-8").split("\n")
        regions, violations = parse_regions("bridge/bot.py", lines,
                                             comment_only=None, tokenized=False)
        self.assertFalse(violations, f"ESTATE region violations: {violations}")
        stripped_lines = strip_regions(lines, regions)
        stripped_src = "\n".join(stripped_lines)
        m = re.search(r"COMMAND_MENU_SPEC = \[(.*?)\n\]", stripped_src, re.DOTALL)
        self.assertIsNotNone(m, "COMMAND_MENU_SPEC not found in stripped source")
        names = re.findall(r'\("(\w+)",', m.group(1))
        self.assertEqual(names, EXPECTED_SPEC_OSS_STRIPPED,
                         f"OSS-stripped COMMAND_MENU_SPEC order mismatch.\n"
                         f"  got:      {names}\n"
                         f"  expected: {EXPECTED_SPEC_OSS_STRIPPED}")


if __name__ == "__main__":
    unittest.main()
