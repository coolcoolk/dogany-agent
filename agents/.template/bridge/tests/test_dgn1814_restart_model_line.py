"""DGN-1814 r2: the restart notice names the model when the live model changed.

Owner copy (2026-10-01 08:19): "🦾 재시작 완료 · 이제 Claude Sonnet 5.5로 답해요."
en: "🦾 Restart complete · Now answering with Claude Sonnet 5.5."

Covers:
  (a) bridge/live_model.py -- record + compare: changed -> display name,
      unchanged -> nothing, first-ever -> nothing, no record -> rc 3 (no
      wait), stale record -> timeout rc 2, [1m]-style suffix is not a change
  (b) sdk_bridge records ONLY system/init model ids
  (c) the REAL self_restart.sh worker (hermetic harness, stub push/launchctl;
      a fake "new bridge process" records its model after the verify spool
      lands, exactly like the real first turn): ko changed / unchanged /
      first-ever / en changed / caller --notice / DGN-706b version
      auto-notice / notice already naming the model / timeout follow-up /
      dry-run
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bridge import live_model

BRIDGE_DIR = Path(__file__).resolve().parents[1]
TEMPLATE = BRIDGE_DIR.parent
SRC_RESTART = BRIDGE_DIR / "self_restart.sh"
LIB = TEMPLATE / "routines" / "lib"
I18N = TEMPLATE / "config" / "i18n"
# The public-build harness ships bridge/ alone; the display table, prefix
# resolver and i18n layer live in the estate tree next to it.
ESTATE = all(p.exists() for p in (
    LIB / "model_display.py", LIB / "agent_prefix.py", LIB / "i18n_lookup.py",
    I18N / "en.json", I18N / "ko.json"))
needs_estate = pytest.mark.skipif(not ESTATE, reason="estate tree (routines/lib, config/i18n) not shipped here")


# ---------------------------------------------------------------- (a) ----

@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(live_model, "_first_seen", None)
    return tmp_path / "state" / "live_model.json"


def _new_process(monkeypatch, boot):
    monkeypatch.setattr(live_model, "_first_seen", None)
    monkeypatch.setattr(live_model, "BOOT", boot)


@needs_estate
def test_changed_model_returns_display_name(fresh, monkeypatch):
    _new_process(monkeypatch, 100.0)
    live_model.observe("claude-sonnet-5", fresh)
    _new_process(monkeypatch, 200.0)
    live_model.observe("claude-sonnet-5-5", fresh)
    assert live_model.changed(fresh, 150, 0, str(LIB)) == (0, "Claude Sonnet 5.5")
    rec = live_model.read_record(fresh)
    assert rec["prev"] == "claude-sonnet-5" and rec["model"] == "claude-sonnet-5-5"


def test_unchanged_model_returns_nothing(fresh, monkeypatch):
    _new_process(monkeypatch, 100.0)
    live_model.observe("claude-sonnet-5-5", fresh)
    _new_process(monkeypatch, 200.0)
    live_model.observe("claude-sonnet-5-5[1m]", fresh)
    assert live_model.changed(fresh, 150, 0, str(LIB)) == (0, "")


def test_first_ever_run_records_without_announcing(fresh, monkeypatch):
    assert live_model.changed(fresh, 0, 5, str(LIB)) == (3, "")  # no wait at all
    _new_process(monkeypatch, 200.0)
    live_model.observe("claude-sonnet-5-5", fresh)
    assert live_model.read_record(fresh)["prev"] == ""
    assert live_model.changed(fresh, 150, 0, str(LIB)) == (0, "")


def test_stale_record_times_out(fresh, monkeypatch):
    _new_process(monkeypatch, 100.0)
    live_model.observe("claude-sonnet-5", fresh)
    t0 = time.time()
    assert live_model.changed(fresh, 150, 0.3, str(LIB), interval=0.05) == (2, "")
    assert time.time() - t0 < 2


def test_later_switch_in_same_process_updates_model_not_first(fresh, monkeypatch):
    _new_process(monkeypatch, 100.0)
    live_model.observe("claude-sonnet-5-5", fresh)
    live_model.observe("claude-opus-5-5", fresh)
    rec = live_model.read_record(fresh)
    assert rec["first_model"] == "claude-sonnet-5-5" and rec["model"] == "claude-opus-5-5"


@needs_estate
def test_unknown_id_falls_back_to_raw(fresh, monkeypatch):
    _new_process(monkeypatch, 100.0)
    live_model.observe("claude-sonnet-5", fresh)
    _new_process(monkeypatch, 200.0)
    live_model.observe("mystery-model", fresh)
    assert live_model.changed(fresh, 150, 0, str(LIB)) == (0, "mystery-model")


# ---------------------------------------------------------------- (b) ----

def test_sdk_bridge_records_only_init_messages(fresh, monkeypatch):
    from bridge import sdk_bridge

    monkeypatch.setattr(sdk_bridge.live_model, "state_path", lambda _d: fresh)
    sdk_bridge._observe_live_model(
        SimpleNamespace(subtype="compact_boundary", data={"model": "claude-x-1"}))
    assert not fresh.exists()
    sdk_bridge._observe_live_model(
        SimpleNamespace(subtype="init", data={"model": "claude-sonnet-5-5", "session_id": "s"}))
    assert live_model.read_record(fresh)["model"] == "claude-sonnet-5-5"


def test_both_system_message_paths_call_the_recorder():
    src = (BRIDGE_DIR / "sdk_bridge.py").read_text(encoding="utf-8")
    assert src.count("_observe_live_model(msg)") == 2


# ---------------------------------------------------------------- (c) ----

KILL_OK = shutil.which("python3") is not None


def _build_instance(root: Path, lang: str, prefix: str = "🦾") -> None:
    br = root / "bridge"
    (br / "venv" / "bin").mkdir(parents=True)
    (root / ".telegram_bot" / "logs").mkdir(parents=True)
    (root / ".telegram_bot" / "state").mkdir(parents=True)
    (root / "routines" / "lib").mkdir(parents=True)
    (root / "config" / "i18n").mkdir(parents=True)
    src = SRC_RESTART.read_text(encoding="utf-8").replace("__AGENT_NAME__", "dgn1814t")
    (br / "self_restart.sh").write_text(src, encoding="utf-8")
    (br / "self_restart.sh").chmod(0o755)
    shutil.copy(BRIDGE_DIR / "live_model.py", br / "live_model.py")
    for f in ("agent_prefix.py", "i18n_lookup.py", "model_display.py", "dispatch-model-display.tsv"):
        shutil.copy(LIB / f, root / "routines" / "lib" / f)
    for f in ("en.json", "ko.json"):
        shutil.copy(I18N / f, root / "config" / "i18n" / f)
    (root / "config" / "agent.conf").write_text("AGENT_LANG=%s\n" % lang, encoding="utf-8")
    # ESTATE:BEGIN DGN-1814 the instance identity file is estate-only; every caller of this helper is needs_estate-skipped in the public tree
    (root / ".instance.conf").write_text(
        "DOGANY_AGENT_PREFIX=%s\nDOGANY_AGENT_NAME=dgn1814t\n" % prefix, encoding="utf-8")
    # ESTATE:END
    push = root / "routines" / "push.sh"
    push.write_text(textwrap.dedent("""\
        #!/bin/bash
        txt=""
        while [[ $# -gt 0 ]]; do case "$1" in --text) txt="$2"; shift 2;; *) shift;; esac; done
        printf '%s\\0' "$txt" >> "$PUSHLOG"
        """), encoding="utf-8")
    push.chmod(0o755)
    (root / ".telegram_bot" / ".env").write_text("", encoding="utf-8")
    stub = br / "venv" / "bin" / "python"
    stub.write_text("#!/bin/bash\necho selfcheck stub\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)


def _seed_prior(root: Path, model: str) -> None:
    (root / ".telegram_bot" / "state" / "live_model.json").write_text(json.dumps(
        {"model": model, "first_model": model, "prev": "", "boot": 1.0}), encoding="utf-8")


OBSERVER = textwrap.dedent("""\
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import live_model
    live_model.observe(sys.argv[2], Path(sys.argv[3]))
    """)


def _run_worker(tmp: Path, root: Path, new_model, extra=(), env_extra=None,
                observe_after_first_push=False, dry=False):
    """Drive the real worker; a fake new process records `new_model` once the
    verify spool lands (or after the first push, for the timeout path)."""
    fakebin = tmp / "fakebin"
    fakebin.mkdir(exist_ok=True)
    count = tmp / "lc.count"
    count.write_text("")
    (fakebin / "launchctl").write_text(textwrap.dedent(f"""\
        #!/bin/bash
        if [[ "$1" == "list" ]]; then
          echo x >> "{count}"
          if [[ "$(wc -l < "{count}" | tr -d ' ')" -le 1 ]]; then
            echo "99999998	0	com.telegram-skill-bot.dgn1814t"
          else
            echo "99999997	0	com.telegram-skill-bot.dgn1814t"
          fi
        fi
        """), encoding="utf-8")
    (fakebin / "launchctl").chmod(0o755)
    pushlog = tmp / "pushes.log"
    pushlog.write_bytes(b"")
    state = root / ".telegram_bot" / "state"
    inbox = root / ".telegram_bot" / "session-inbox"
    env = dict(os.environ, PATH="%s:%s" % (fakebin, os.environ["PATH"]), PUSHLOG=str(pushlog),
               DGN1814_MODEL_WAIT="15", DGN1814_MODEL_FOLLOWUP_WAIT="15")
    env.update(env_extra or {})
    args = ["bash", str(root / "bridge" / "self_restart.sh"), "--_worker", "--reason", "dgn1814 test",
            "--label", "com.telegram-skill-bot.dgn1814t", "--env", str(root / ".telegram_bot" / ".env"),
            "--delay", "0", *extra]
    if dry:
        args.append("--dry-run")
    t0 = time.time()
    proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    marker_seen = observed = dry
    deadline = time.time() + 90
    while proc.poll() is None and time.time() < deadline:
        if not marker_seen and (state / "restart-pending.marker").exists():
            with open(root / ".telegram_bot" / "logs" / "bot.log", "a") as fh:
                fh.write("Bot is running\n")
            marker_seen = True
        if new_model and not observed:
            ready = (pushlog.stat().st_size > 0) if observe_after_first_push \
                else any(inbox.glob("restart-verify-*.md"))
            if ready:
                subprocess.run([sys.executable, "-c", OBSERVER, str(BRIDGE_DIR), new_model,
                                str(state / "live_model.json")], check=True)
                observed = True
        time.sleep(0.05)
    out = proc.communicate(timeout=30)[0].decode("utf-8", "replace")
    pushes = [p for p in pushlog.read_bytes().decode("utf-8").split("\0") if p]
    return pushes, out, time.time() - t0


@pytest.fixture()
def inst(tmp_path):
    def make(lang="ko", prefix="🦾"):
        root = tmp_path / ("inst-%s" % lang)
        _build_instance(root, lang, prefix)
        return root
    return make


pytestmark_worker = pytest.mark.skipif(not (KILL_OK and ESTATE), reason="python3 or estate tree missing")


@pytestmark_worker
def test_worker_ko_changed_model_names_it(tmp_path, inst):
    root = inst("ko")
    _seed_prior(root, "claude-sonnet-5")
    pushes, out, _ = _run_worker(tmp_path, root, "claude-sonnet-5-5")
    assert pushes == ["🦾 재시작 완료 · 이제 Claude Sonnet 5.5로 답해요."], out


@pytestmark_worker
def test_worker_ko_unchanged_model_keeps_todays_notice(tmp_path, inst):
    root = inst("ko")
    _seed_prior(root, "claude-sonnet-5-5")
    pushes, out, _ = _run_worker(tmp_path, root, "claude-sonnet-5-5")
    assert pushes == ["🦾 재시작 완료"], out


@pytestmark_worker
def test_worker_first_ever_run_no_line_no_wait(tmp_path, inst):
    root = inst("ko")
    pushes, out, took = _run_worker(tmp_path, root, None,
                                    env_extra={"DGN1814_MODEL_WAIT": "30"})
    assert pushes == ["🦾 재시작 완료"], out
    assert took < 20, "first-ever run must not wait for a model (took %.1fs)" % took


@pytestmark_worker
def test_worker_en_changed_model(tmp_path, inst):
    root = inst("en")
    _seed_prior(root, "claude-sonnet-5")
    pushes, out, _ = _run_worker(tmp_path, root, "claude-sonnet-5-5")
    assert pushes == ["🦾 Restart complete · Now answering with Claude Sonnet 5.5."], out


@pytestmark_worker
def test_worker_ko_final_consonant_particle(tmp_path, inst):
    root = inst("ko")
    _seed_prior(root, "claude-opus-5-5")
    pushes, out, _ = _run_worker(tmp_path, root, "claude-opus-6")
    assert pushes == ["🦾 재시작 완료 · 이제 Claude Opus 6으로 답해요."], out


@pytestmark_worker
def test_worker_caller_notice_gets_line_on_headline_once(tmp_path, inst):
    root = inst("ko")
    _seed_prior(root, "claude-sonnet-5")
    notice = "재시작 완료 · v2.6.0 업데이트 완료\n<blockquote expandable>요약\n- a</blockquote>"
    pushes, out, _ = _run_worker(tmp_path, root, "claude-sonnet-5-5", extra=("--notice", notice))
    assert pushes == ["🦾 재시작 완료 · v2.6.0 업데이트 완료 · 이제 Claude Sonnet 5.5로 답해요."
                      "\n<blockquote expandable>요약\n- a</blockquote>"], out
    assert pushes[0].count("Claude Sonnet 5.5") == 1


@pytestmark_worker
def test_worker_caller_notice_already_naming_model_is_untouched(tmp_path, inst):
    root = inst("ko")
    _seed_prior(root, "claude-sonnet-5")
    notice = "재시작했어요. 이제 Claude Sonnet 5.5예요."
    pushes, out, _ = _run_worker(tmp_path, root, "claude-sonnet-5-5", extra=("--notice", notice))
    assert pushes == ["🦾 " + notice], out


def _auto_notice(root: Path, tmp: Path) -> str:
    """Run the REAL maybe_compose_update_notice (DGN-706b) with the REAL i18n
    helpers in the instance's AGENT_LANG; return the composed --notice."""
    # ESTATE:BEGIN DGN-1814 the instance identity file is estate-only; every caller of this helper is needs_estate-skipped in the public tree
    (root / ".instance.conf").write_text(
        "DOGANY_AGENT_PREFIX=🦾\nDOGANY_FW_VERSION=2.6.0\n", encoding="utf-8")
    # ESTATE:END
    rel = root / "product" / "releases"
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "v2.6.0.md").write_text("# v2.6.0\n\n## Summary\n\n- machine CLI\n\n---\n", encoding="utf-8")
    src = SRC_RESTART.read_text(encoding="utf-8")

    def fn(name):
        body = src[src.index(name + "() {"):]
        return body[:body.index("\n}\n") + 3]
    i18n_get = src[src.index("i18n_get() {"):]
    i18n_get = i18n_get[:i18n_get.index("\n") + 1]
    lang = src[src.index('AGENT_LANG="$(sed'):]
    lang = lang[:lang.index("i18n_get() {")]
    script = ('INSTANCE_ROOT=%s; VER_MARKER=%s; NOTICE=""\n%s%s%s%s\n'
              'maybe_compose_update_notice >/dev/null; printf "%%s" "$NOTICE"'
              % (root, tmp / "vermark", lang, i18n_get, fn("i18n_fmt"), fn("maybe_compose_update_notice")))
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout


def _hangul(s: str) -> bool:
    return any("\uac00" <= ch <= "\ud7a3" or "\u3131" <= ch <= "\u318e" for ch in s)


@pytestmark_worker
def test_worker_version_auto_notice_composes_ko(tmp_path, inst):
    """DGN-706b + DGN-1814 r2b: on a ko instance the auto-notice headline is
    the owner-locked ko form (same key as self-update.sh) and the model line
    joins it -- the WHOLE first line is Korean (only names/version stay)."""
    root = inst("ko")
    _seed_prior(root, "claude-sonnet-5")
    notice = _auto_notice(root, tmp_path)
    assert notice.startswith("재시작 완료 · v2.6.0 업데이트 완료\n<blockquote expandable><b>▸ 업데이트 요약</b>\n")
    pushes, out, _ = _run_worker(tmp_path, root, "claude-sonnet-5-5", extra=("--notice", notice))
    assert len(pushes) == 1, out
    head, _, rest = pushes[0].partition("\n")
    assert head == "🦾 재시작 완료 · v2.6.0 업데이트 완료 · 이제 Claude Sonnet 5.5로 답해요."
    leftover = head.replace("Claude Sonnet 5.5", "").replace("v2.6.0", "")
    assert not any("a" <= c.lower() <= "z" for c in leftover), head
    assert rest.startswith("<blockquote expandable>") and "- machine CLI" in rest
    assert pushes[0].count("Claude Sonnet 5.5") == 1


@pytestmark_worker
def test_worker_version_auto_notice_composes_en(tmp_path, inst):
    root = inst("en")
    _seed_prior(root, "claude-sonnet-5")
    notice = _auto_notice(root, tmp_path)
    assert notice.startswith("Restart complete · v2.6.0 update complete\n<blockquote expandable><b>▸ At a glance</b>\n")
    pushes, out, _ = _run_worker(tmp_path, root, "claude-sonnet-5-5", extra=("--notice", notice))
    assert len(pushes) == 1, out
    head = pushes[0].partition("\n")[0]
    assert head == "🦾 Restart complete · v2.6.0 update complete · Now answering with Claude Sonnet 5.5."
    assert not _hangul(pushes[0])


@pytestmark_worker
def test_worker_no_i18n_files_falls_back_to_english(tmp_path, inst):
    """A ko instance whose locale files are missing gets the English in-code
    defaults -- never a half-Korean line."""
    root = inst("ko")
    shutil.rmtree(root / "config" / "i18n")
    _seed_prior(root, "claude-sonnet-5")
    notice = _auto_notice(root, tmp_path)
    assert notice.startswith("Restart complete · v2.6.0 update complete\n<blockquote expandable><b>▸ At a glance</b>\n")
    pushes, out, _ = _run_worker(tmp_path, root, "claude-sonnet-5-5", extra=("--notice", notice))
    assert pushes[0].partition("\n")[0] == \
        "🦾 Restart complete · v2.6.0 update complete · Now answering with Claude Sonnet 5.5."
    assert not _hangul(pushes[0])
    pushes, out, _ = _run_worker(tmp_path, root, None)
    assert pushes == ["🦾 Restart complete"], out


@pytestmark_worker
@pytest.mark.parametrize("lang,label,derived,dry", [
    ("ko", "🦾 재시작 완료 — 직전 작업 이어서 진행합니다.",
     "🦾 재시작 완료 — 보고서 정리 이어서 진행합니다.",
     "🦾 [DRY-RUN] 재시작 통보 경로 정상: dgn1814 test"),
    ("en", "🦾 Restart complete — continuing the previous task.",
     "🦾 Restart complete — continuing 보고서 정리.",
     "🦾 [DRY-RUN] Restart notice path OK: dgn1814 test"),
])
def test_worker_resume_and_dry_run_lines_follow_lang(tmp_path, inst, lang, label, derived, dry):
    root = inst(lang)
    pushes, out, _ = _run_worker(tmp_path, root, None, extra=("--resume-intent", ": next step"))
    assert pushes == [label], out
    pushes, out, _ = _run_worker(tmp_path, root, None, extra=("--resume-intent", "보고서 정리: next"))
    assert pushes == [derived], out
    pushes, out, _ = _run_worker(tmp_path, root, None, dry=True)
    assert pushes == [dry], out


# ESTATE:BEGIN routines/self-update.sh is the estate updater; the public bridge does not ship it (these cases skip there anyway)
SELF_UPDATE = TEMPLATE / "routines" / "self-update.sh"
needs_self_update = pytest.mark.skipif(not SELF_UPDATE.exists(), reason="routines/self-update.sh not shipped here")


@needs_self_update
def test_self_update_auto_notice_shares_the_keys():
    """routines/self-update.sh renders the same restart-notice header through
    the same restart.* keys (one copy for both paths)."""
    su = SELF_UPDATE.read_text(encoding="utf-8")
    sh = SRC_RESTART.read_text(encoding="utf-8")
    for key in ("restart.update_done", "restart.fold_summary"):
        assert "i18n_fmt %s " % key in su and "i18n_fmt %s " % key in sh
# ESTATE:END


def _code_lines(text: str):
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def test_no_hangul_in_self_restart():
    """No Korean literal anywhere in self_restart.sh (code or comment)."""
    sh = SRC_RESTART.read_text(encoding="utf-8")
    assert not _hangul(sh), [ln for ln in sh.splitlines() if _hangul(ln)]


# ESTATE:BEGIN reads the estate updater (routines/self-update.sh), canonical-only
@needs_self_update
def test_no_hangul_in_self_update_owner_lines():
    """None in the owner-notice code of self-update.sh (restart_notice +
    maybe_restart); its operator log lines go through the bilingual msg()
    and are excluded, as are comments."""
    su = SELF_UPDATE.read_text(encoding="utf-8")
    region = su[su.index("restart_notice() {"):su.index("# DGN-1758: render-only mode (see header)")]
    bad = []
    in_msg = False
    for ln in _code_lines(region):
        st = ln.lstrip()
        if st.startswith("msg "):
            in_msg = ln.rstrip().endswith("\\")
            continue
        if in_msg:
            in_msg = ln.rstrip().endswith("\\")
            continue
        if _hangul(ln):
            bad.append(ln)
    assert not bad, bad
# ESTATE:END


@pytestmark_worker
def test_worker_timeout_sends_notice_then_one_followup(tmp_path, inst):
    root = inst("ko")
    _seed_prior(root, "claude-sonnet-5")
    pushes, out, _ = _run_worker(tmp_path, root, "claude-sonnet-5-5",
                                 env_extra={"DGN1814_MODEL_WAIT": "1"},
                                 observe_after_first_push=True)
    assert pushes == ["🦾 재시작 완료", "🦾 이제 Claude Sonnet 5.5로 답해요."], out


@pytestmark_worker
def test_worker_dry_run_has_no_line_and_no_wait(tmp_path, inst):
    root = inst("ko")
    _seed_prior(root, "claude-sonnet-5")
    pushes, out, took = _run_worker(tmp_path, root, None, dry=True,
                                    env_extra={"DGN1814_MODEL_WAIT": "30"})
    assert len(pushes) == 1 and "Claude" not in pushes[0], out
    assert took < 20


def test_no_korean_literal_in_new_code():
    src = (BRIDGE_DIR / "live_model.py").read_text(encoding="utf-8")
    sh = SRC_RESTART.read_text(encoding="utf-8")
    block = sh[sh.index("# DGN-1814-BEGIN"):sh.index("# DGN-1814-END")]
    hangul = lambda s: any("가" <= ch <= "힣" for ch in s)
    assert not hangul(src) and not hangul(block)
