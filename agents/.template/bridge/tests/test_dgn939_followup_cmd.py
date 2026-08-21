"""DGN-939 item2 (Skull handoff 6EXRS2Z1): idrill `followup_cmd` primitive.

Contract: the arm file may declare an OPTIONAL `followup_cmd` list[str] argv.
After the terminal cmd (`cmd` / `cmd_skip2`) fires with exit 0, the bridge
executes `followup_cmd` under the exact same security invariants (list-form
subprocess, no shell, no PATH lookup, 15s timeout) with the same exact-match
{i} substitution over the same capture list, and posts its STDOUT to the
owner chat as a NEW inline message (never an edit). Absent/empty declaration
-> byte-identical to today (zero followup side effects).

Covered invariants:

  (a)  followup_cmd absent -> no post, single subprocess, today's behavior.
  (a2) followup_cmd empty list -> same as absent (byte-identical).
  (b)  terminal exit 0 + followup exit 0 -> STDOUT posted as a NEW message
       (reply_text), confirmation edit unchanged, post is NOT an edit.
  (b2) followup argv receives the same {i} capture substitution as cmd.
  (c)  terminal cmd exit != 0 -> followup binary NEVER invoked, no post.
  (d)  mutual-exclusion guard: _idrill_fire_cmd_n alone (the model-turn-side
       primitive) never executes followup_cmd and never posts -- the followup
       exists only on the callback fire path (_idrill_fire_final).
  (e)  security: followup subprocess is list-form, no shell=True, timeout=15.
  (f)  followup timeout -> fail-soft: no post, confirmation stands.
  (g)  followup nonzero exit -> fail-soft: no post, confirmation stands.
  (h)  malformed followup_cmd (non-list / non-str elements) -> no followup
       subprocess, no post, confirmation stands.
  (i)  followup token out of range ({3} on a 2-step arm) -> no followup
       subprocess, no post, confirmation stands.
  (j)  empty followup STDOUT -> no post.
  (k)  skip path: followup fires after cmd_skip2 success ({1} only).
  (k2) skip path with a {2} followup token -> malformed, fail-soft, no post.
  (l)  real exec (no mocks): followup STDOUT actually posted.
  (m)  HTML post failure -> plain-text fallback post (message not dropped).
  (n)  render (not post) raising must NOT escape _idrill_fire_final.
  (o)  argv[0] absolute-path gate (contract section 4): a relative/empty
       argv[0] is refused before any subprocess -- cmd and followup share it.
"""

import asyncio
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

import bridge.bot as bot_mod
from bridge.bot import TelegramBot
from bridge import messages


def run(coro):
    return asyncio.run(coro)


class FakeMessage:
    """Message mock recording new-message posts (reply_text)."""

    def __init__(self):
        self.reply_markup = "EXISTING-MARKUP"
        self.posts = []  # (text, kwargs) from reply_text

    async def reply_text(self, text, **kwargs):
        self.posts.append((text, kwargs))


class FakeQuery:
    """Query mock recording edits (confirmation) and posts (followup)."""

    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.text_edits = []

    async def edit_message_text(self, text, **kwargs):
        self.text_edits.append(text)

    async def edit_message_reply_markup(self, *, reply_markup=None, **kwargs):
        pass


@pytest.fixture
def bot():
    return TelegramBot.__new__(TelegramBot)


@pytest.fixture
def arm_dir(tmp_path):
    cb = tmp_path / "files" / "program" / ".idrill-arm"
    cb.mkdir(parents=True)
    with patch.object(bot_mod, "PROJECT_ROOT", tmp_path):
        yield cb


CMD_BIN = "/opt/lifekit/bin/record-entry"
FOLLOWUP_BIN = "/opt/lifekit/bin/set-table"

ARM_BASE = {
    "session_id": "sess-abc123",
    "cmd": [CMD_BIN, "sess-abc123", "{1}", "--intensity", "{2}"],
    "cmd_skip2": [CMD_BIN, "sess-abc123", "{1}"],
    "step_buttons": {
        "1": [["8", "8"], ["9", "9"], ["10", "10"]],
        "2": [["0", "0"], ["1", "1"], ["2", "2"], ["skip", "skip"]],
    },
    "step_validate": {
        "1": r"^[0-9]{1,4}$",
        "2": r"^([0-2]|skip)$",
    },
    "_pending_step1": "8",
}


def write_arm(arm_dir, arm_id="abcd1234", extra=None, omit=None):
    content = dict(ARM_BASE)
    if extra:
        content.update(extra)
    for key in (omit or ()):
        content.pop(key, None)
    path = arm_dir / arm_id
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def make_proc(returncode=0, stdout="", stderr=""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def patch_run_by_bin(monkeypatch, results, calls):
    """subprocess.run stub routing by argv[0]; records (argv, kwargs)."""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return results[argv[0]]

    monkeypatch.setattr(bot_mod.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# (a) absent followup_cmd -> byte-identical to today
# ---------------------------------------------------------------------------

def test_absent_followup_no_post_single_subprocess(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id)  # no followup_cmd
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {CMD_BIN: make_proc(0, "ok")}, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert len(calls) == 1, "exactly the terminal cmd, nothing else"
    assert q.message.posts == [], "no new message posted"
    assert len(q.text_edits) == 1, "confirmation edit only"


def test_empty_followup_list_same_as_absent(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"followup_cmd": []})
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {CMD_BIN: make_proc(0, "ok")}, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert len(calls) == 1
    assert q.message.posts == []


# ---------------------------------------------------------------------------
# (b) exit 0 -> followup STDOUT posted as a NEW message
# ---------------------------------------------------------------------------

def test_followup_stdout_posted_as_new_message(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "sess-abc123", "{1}", "{2}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0, "recorded"),
        FOLLOWUP_BIN: make_proc(0, "SET 1 done - next SET 2\n"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    # terminal cmd first, followup second
    assert [argv[0] for argv, _ in calls] == [CMD_BIN, FOLLOWUP_BIN]
    # confirmation is still the (single) in-place edit
    assert len(q.text_edits) == 1
    # followup STDOUT posted as ONE new message, not an edit
    assert len(q.message.posts) == 1
    text, kwargs = q.message.posts[0]
    assert "SET 1 done" in text
    assert kwargs.get("parse_mode") == "HTML"


def test_followup_argv_gets_same_capture_substitution(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "{2}", "verbatim-{1}", "{1}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:1")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(0, "tbl"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    followup_argv = calls[1][0]
    # exact {i} elements substituted; substring token stays verbatim
    assert followup_argv == [FOLLOWUP_BIN, "1", "verbatim-{1}", "8"]


# ---------------------------------------------------------------------------
# (c) terminal cmd failure -> followup never invoked
# ---------------------------------------------------------------------------

def test_terminal_failure_suppresses_followup(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "{1}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(3, "", "boom"),
        FOLLOWUP_BIN: make_proc(0, "never"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert [argv[0] for argv, _ in calls] == [CMD_BIN], \
        "followup must not run after a failed terminal cmd"
    assert q.message.posts == []
    assert q.text_edits == [messages.IDRILL_ERROR]


# ---------------------------------------------------------------------------
# (d) mutual-exclusion guard: the fire primitive itself never posts
# ---------------------------------------------------------------------------

def test_fire_cmd_n_never_runs_followup_or_posts(bot, monkeypatch):
    """_idrill_fire_cmd_n is the shared fire primitive; the followup post is
    wired ONLY into the callback fire path (_idrill_fire_final). Any other
    caller (e.g. a future model-turn-side reuse) must get zero followup side
    effects -- the model-turn set-add path keeps its own PostToolUse hook,
    and this guard is what makes a double-fire structurally impossible."""
    arm = dict(ARM_BASE)
    arm["followup_cmd"] = [FOLLOWUP_BIN, "{1}"]
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0, "ok"),
        FOLLOWUP_BIN: make_proc(0, "table"),
    }, calls)

    ok = run(bot._idrill_fire_cmd_n(arm, [("1", "8"), ("2", "2")], False))

    assert ok is True
    assert [argv[0] for argv, _ in calls] == [CMD_BIN], \
        "fire primitive must execute the terminal argv ONLY"


def test_followup_post_helper_only_called_from_fire_final(bot, arm_dir,
                                                          monkeypatch):
    """Structural double-check of the guard: during a full callback fire the
    followup post happens exactly once, and the arm is already consumed --
    so a re-entry (or a hook-side second author) cannot re-fire it."""
    arm_id = "abcd1234"
    arm_path = write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "{1}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(0, "table"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))
    assert len(q.message.posts) == 1
    assert not arm_path.exists(), "arm consumed -> no second fire possible"

    # second tap on the same (consumed) arm: EXPIRED, no fire, no post
    q2 = FakeQuery(f"idrill:{arm_id}:2:2")
    run(bot._handle_idrill_callback(q2, q2.data))
    assert q2.message.posts == []
    assert q2.text_edits == [messages.IDRILL_ARM_EXPIRED]
    assert [argv[0] for argv, _ in calls] == [CMD_BIN, FOLLOWUP_BIN]


# ---------------------------------------------------------------------------
# (e) security invariants: list-form, no shell, 15s timeout
# ---------------------------------------------------------------------------

def test_followup_subprocess_list_form_no_shell_timeout(bot, arm_dir,
                                                        monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "$(rm -rf /)", "; echo pwned", "{1}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(0, "t"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    followup_argv, followup_kwargs = calls[1]
    assert isinstance(followup_argv, list), "list-form argv, never a string"
    # shell metacharacters stay inert single argv elements
    assert "$(rm -rf /)" in followup_argv
    assert "; echo pwned" in followup_argv
    assert followup_kwargs.get("shell") is not True
    assert "shell" not in followup_kwargs
    assert followup_kwargs.get("timeout") == 15


# ---------------------------------------------------------------------------
# (f)/(g) followup timeout / nonzero exit -> fail-soft, no post
# ---------------------------------------------------------------------------

def test_followup_timeout_fail_soft(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "{1}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")

    def fake_run(argv, **kwargs):
        if argv[0] == FOLLOWUP_BIN:
            raise bot_mod.subprocess.TimeoutExpired(argv, 15)
        return make_proc(0)

    monkeypatch.setattr(bot_mod.subprocess, "run", fake_run)

    run(bot._handle_idrill_callback(q, q.data))

    assert q.message.posts == [], "timed-out followup must not post"
    assert len(q.text_edits) == 1, "confirmation stands"
    assert q.text_edits[0] != messages.IDRILL_ERROR


def test_followup_nonzero_exit_fail_soft(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "{1}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(1, "partial", "err"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert q.message.posts == []
    assert len(q.text_edits) == 1
    assert q.text_edits[0] != messages.IDRILL_ERROR


# ---------------------------------------------------------------------------
# (h)/(i) malformed followup declarations -> fail-soft, no subprocess
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "not-a-list",
    [FOLLOWUP_BIN, 42],
    {"argv": [FOLLOWUP_BIN]},
])
def test_malformed_followup_fail_soft(bot, arm_dir, monkeypatch, bad):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"followup_cmd": bad})
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(0, "t"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert [argv[0] for argv, _ in calls] == [CMD_BIN]
    assert q.message.posts == []
    assert len(q.text_edits) == 1
    assert q.text_edits[0] != messages.IDRILL_ERROR


def test_followup_token_out_of_range_fail_soft(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "{3}"],  # 2-step arm: no 3rd capture
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(0, "t"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert [argv[0] for argv, _ in calls] == [CMD_BIN], \
        "malformed followup token -> no followup subprocess"
    assert q.message.posts == []
    assert len(q.text_edits) == 1
    assert q.text_edits[0] != messages.IDRILL_ERROR


# ---------------------------------------------------------------------------
# (j) empty STDOUT -> no post
# ---------------------------------------------------------------------------

def test_followup_empty_stdout_no_post(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "{1}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(0, "   \n"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert len(calls) == 2, "followup ran"
    assert q.message.posts == [], "blank STDOUT -> nothing to post"


# ---------------------------------------------------------------------------
# (k) skip path
# ---------------------------------------------------------------------------

def test_followup_fires_after_skip_variant(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "{1}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:skip")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(0, "skipped-set table"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert [argv[0] for argv, _ in calls] == [CMD_BIN, FOLLOWUP_BIN]
    assert calls[1][0] == [FOLLOWUP_BIN, "8"]
    assert len(q.message.posts) == 1


def test_followup_step2_token_on_skip_fail_soft(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "{2}"],  # skipped capture is absent
    })
    q = FakeQuery(f"idrill:{arm_id}:2:skip")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(0, "t"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert [argv[0] for argv, _ in calls] == [CMD_BIN]
    assert q.message.posts == []
    assert len(q.text_edits) == 1
    assert q.text_edits[0] != messages.IDRILL_ERROR


# ---------------------------------------------------------------------------
# (l) real exec, no subprocess mocks (no shell, no PATH, real STDOUT)
# ---------------------------------------------------------------------------

def test_followup_real_exec_posts_stdout(bot, arm_dir):
    arm_id = "abcd1234"
    py = sys.executable  # absolute path, no PATH lookup
    write_arm(arm_dir, arm_id, extra={
        "cmd": [py, "-c", "import sys; sys.exit(0)"],
        "followup_cmd": [
            py, "-c",
            "import sys; print('TBL row ' + sys.argv[1])",
            "{1}",
        ],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")

    run(bot._handle_idrill_callback(q, q.data))

    assert len(q.message.posts) == 1
    text, kwargs = q.message.posts[0]
    assert "TBL row 8" in text
    assert kwargs.get("parse_mode") == "HTML"
    assert len(q.text_edits) == 1  # confirmation edit unaffected


# ---------------------------------------------------------------------------
# (m) HTML post failure -> plain fallback
# ---------------------------------------------------------------------------

def test_followup_html_post_failure_falls_back_plain(bot, arm_dir,
                                                     monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": [FOLLOWUP_BIN, "{1}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")

    attempts = []

    async def flaky_reply(text, **kwargs):
        attempts.append((text, kwargs))
        if kwargs.get("parse_mode") == "HTML":
            raise RuntimeError("bad entities")

    q.message.reply_text = flaky_reply
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(0, "raw table"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert len(attempts) == 2, "HTML attempt then plain fallback"
    assert attempts[0][1].get("parse_mode") == "HTML"
    assert "parse_mode" not in attempts[1][1]
    assert attempts[1][0] == "raw table"


# ---------------------------------------------------------------------------
# (n) render (not post) raising must NOT escape -- self-grill regression
# ---------------------------------------------------------------------------

def test_followup_render_failure_does_not_escape(bot, monkeypatch):
    """A markdown_to_telegram_html / balance_telegram_html failure must be
    absorbed exactly like a post failure: the fire already succeeded, so the
    render raising must never propagate into _idrill_fire_final. Falls back
    to the plain-text post so the STDOUT still reaches the owner."""
    posts = []

    class Q:
        def __init__(self):
            self.message = MagicMock()

            async def rt(text, **kwargs):
                posts.append((text, kwargs))

            self.message.reply_text = rt

    q = Q()
    monkeypatch.setattr(
        bot_mod, "markdown_to_telegram_html",
        MagicMock(side_effect=RuntimeError("render boom")),
    )

    # must not raise
    run(bot._idrill_post_followup(q, "table body"))

    assert len(posts) == 1, "plain fallback post after render failure"
    assert posts[0][0] == "table body"
    assert "parse_mode" not in posts[0][1]


# ---------------------------------------------------------------------------
# (o) argv[0] absolute-path gate (contract section 4) -- cmd + followup share it
# ---------------------------------------------------------------------------

def test_relative_followup_argv0_refused(bot, arm_dir, monkeypatch):
    # followup_cmd with a RELATIVE argv[0] must be refused before any
    # subprocess -- a relative argv[0] would let subprocess.run fall back to a
    # PATH search, defeating the "no PATH lookup" invariant. The terminal cmd
    # (absolute) still fires; the followup is skipped, no post, confirmation
    # stands (fail-soft, not IDRILL_ERROR).
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "followup_cmd": ["set-table", "{1}"],  # relative argv[0]
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {
        CMD_BIN: make_proc(0),
        FOLLOWUP_BIN: make_proc(0, "t"),
    }, calls)

    run(bot._handle_idrill_callback(q, q.data))

    # terminal cmd ran; followup binary NEVER invoked (gate refused it)
    assert [argv[0] for argv, _ in calls] == [CMD_BIN]
    assert q.message.posts == []
    assert len(q.text_edits) == 1
    assert q.text_edits[0] != messages.IDRILL_ERROR


def test_relative_cmd_argv0_refused(bot, arm_dir, monkeypatch):
    # The SAME gate guards the terminal cmd: a relative cmd argv[0] aborts the
    # fire with no subprocess -> IDRILL_ERROR (the fire failed).
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "cmd": ["record-entry", "{1}", "--intensity", "{2}"],  # relative
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")
    calls = []
    patch_run_by_bin(monkeypatch, {CMD_BIN: make_proc(0)}, calls)

    run(bot._handle_idrill_callback(q, q.data))

    assert calls == [], "relative cmd argv[0] -> no subprocess at all"
    assert q.message.posts == []
    assert q.text_edits == [messages.IDRILL_ERROR]


def test_exec_argv_gate_direct_unit(bot):
    # Direct unit on the shared gate: relative -> refused (None), empty ->
    # refused (None). No subprocess is reached in either case.
    async def _probe(argv):
        return await bot._idrill_exec_argv(argv, {"session_id": "s"})

    assert run(_probe(["rel/bin", "x"])) is None  # relative refused
    assert run(_probe([])) is None  # empty refused
