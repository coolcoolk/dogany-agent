"""DGN-986 [c]: /health command -- healthcmd collection + compose contract.

All system observation is fixture-injected: NO launchctl, NO ps, NO network
in any test here (the probe layer is bypassed via collect_report kwargs).

Covered (spec-mandated):
  - zero issues        -> collapsed short form ("all normal" 3-liner + fold)
  - one issue          -> issue block sits at the very top
  - snapshot absent    -> mtime fallback fires with the DOWNGRADED tone
                          (check wording, never the assertive snapshot copy)
  - snapshot present   -> sha drift detected with the assertive copy + CTA
  - jobs.json absent   -> "observer not running" surfaced (never all-normal)
  - stale heartbeat    -> same (a dead observer must not vouch for normality
                          -- the ticket's core failure mode)
  - engine_versions    -> per-kit map preserved, never collapsed (F8)
  - live lookup fails  -> honest "lookup failed" line (DGN-962)
  - menu has 11 cmds, /health directly before /help (D1 owner amendment)
  - handler never touches the session / model (source-level assert)
  - collection writes NOTHING into other instances' trees (read-only rule)
  - display names resolve via the registry; unregistered -> real dir name
"""

import hashlib
import inspect
import json
import plistlib
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from bridge import healthcmd

NOW = datetime(2026, 8, 21, 21, 28, 0)
LSTART = NOW - timedelta(hours=7)
LSTART_PS = LSTART.strftime("%a %b %d %H:%M:%S %Y")
OLD = (NOW - timedelta(days=3)).timestamp()

PREFIX = "com.telegram-skill-bot."


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------

def _make_db(path: Path, user_version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA user_version=%d" % user_version)
    con.commit()
    con.close()


def _utime_tree(root: Path, ts: float) -> None:
    import os
    for p in sorted(root.rglob("*"), reverse=True):
        os.utime(p, (ts, ts))
    os.utime(root, (ts, ts))


def _make_instance(base: Path, name: str, fw="1.39.3", engines=None) -> Path:
    root = base / name
    (root / ".telegram_bot").mkdir(parents=True)
    (root / ".instance.conf").write_text(
        "DOGANY_FW_VERSION=%s\n" % fw, encoding="utf-8")
    (root / ".telegram_bot" / ".env").write_text("K=V\n", encoding="utf-8")
    for stem, ver in (engines or {}).items():
        _make_db(root / "database" / ("%s.db" % stem), ver)
    _utime_tree(root, OLD)  # default: configs older than lstart (no drift)
    return root


def _make_plist(la_dir: Path, slug: str, root: Path) -> str:
    label = PREFIX + slug
    la_dir.mkdir(parents=True, exist_ok=True)
    with open(la_dir / (label + ".newbridge.plist"), "wb") as f:
        plistlib.dump({
            "Label": label,
            "ProgramArguments": ["/bin/bash", str(root / "bridge" / "start.sh"),
                                 "--path", str(root), "--_launchd_child"],
            "WorkingDirectory": "/Users/somebody",  # useless by design (F6)
        }, f)
    return label


def _write_snapshot(root: Path, pid: int, fw="1.39.3", env_sha_ok=True,
                    snap_fw=None) -> None:
    env_path = root / ".telegram_bot" / ".env"
    sha = hashlib.sha256(env_path.read_bytes()).hexdigest()
    if not env_sha_ok:
        sha = "0" * 64
    snap = {
        "schema": 1,
        "label": "x",
        "pid": pid,
        "started_at": LSTART.isoformat(),
        "fw_version": snap_fw or fw,
        "engine_versions": {},
        "claude_cli": {},
        "env_files": [{"path": str(env_path), "sha256": sha}],
    }
    (root / ".telegram_bot" / "runtime-snapshot.json").write_text(
        json.dumps(snap), encoding="utf-8")


REGISTRY_MD = """
| slug | display | emoji | crew | role | path | endpoint | state |
|:--|:--|:--|:--|:--|:--|:--|:--|
| metal | Kim Metal (Metal Kim) | x | dev | leader | `{metal}` | 1 | live |
| kojeni | Kojeni-D (PoC) | x | poc | leader | `{kojeni}` | 2 | live |
"""


def _healthy_jobs_state(sched=68):
    return {
        "schema": 1,
        "heartbeat_at": NOW.isoformat(),
        "warnings": [],
        "parse_errors": [],
        "counts": {"scheduled": sched, "bridge": 3, "resident_service": 2},
        "jobs": {},
    }


class Estate:
    """One assembled fixture estate under tmp_path."""

    def __init__(self, tmp_path: Path, jobs_state="healthy"):
        self.base = tmp_path
        self.la = tmp_path / "LaunchAgents"
        self.self_root = _make_instance(tmp_path, "metal", engines={"lifekit": 11})
        self.kojeni = _make_instance(tmp_path, "Kojeni",
                                     engines={"lifekit": 29, "portfolio": 3})
        self.digear = _make_instance(tmp_path, "DigEar", engines={})
        self.labels = {
            "metal": _make_plist(self.la, "metal", self.self_root),
            "kojeni": _make_plist(self.la, "kojeni", self.kojeni),
            "digear": _make_plist(self.la, "digear", self.digear),
        }
        _utime_tree(self.la, OLD)
        self.pids = {"metal": 100, "kojeni": 200, "digear": 300}
        self.registry = tmp_path / "MANAGED-TARGETS.md"
        self.registry.write_text(
            REGISTRY_MD.format(metal=self.self_root, kojeni=self.kojeni),
            encoding="utf-8")
        self.shared_env = tmp_path / "rules.env"
        self.shared_env.write_text("SHARED=1\n", encoding="utf-8")
        import os
        os.utime(self.shared_env, (OLD, OLD))
        self.state_path = tmp_path / "observer" / "jobs.json"
        if jobs_state == "healthy":
            self.write_jobs(_healthy_jobs_state())
        self.choice_path = tmp_path / "claude-install-choice"  # absent default
        # self gets a clean valid snapshot; estate has none (mtime fallback,
        # mirrors the live pre-deploy reality).
        _write_snapshot(self.self_root, self.pids["metal"])

    def write_jobs(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def launchctl_output(self, down=()):
        lines = []
        for name, label in self.labels.items():
            pid = "-" if name in down else str(self.pids[name])
            lines.append("%s\t0\t%s" % (pid, label))
        return "\n".join(lines)

    def ps_output(self, down=()):
        return "\n".join(
            "%5d %s" % (self.pids[n], LSTART_PS)
            for n in self.pids if n not in down)

    def collect(self, **kw):
        args = dict(
            now=NOW,
            launch_agents_dir=self.la,
            launchctl_list_output=self.launchctl_output(),
            ps_lstart_output=self.ps_output(),
            self_root=self.self_root,
            registry_path=self.registry,
            observer_state_path=self.state_path,
            install_choice_path=self.choice_path,
            shared_env_path=str(self.shared_env),
            cli_info={"resolved_path": "/u/.local/bin/claude",
                      "version": "2.1.238 (Claude Code)",
                      "install_method": "native"},
            auto_updates=True,
            latest_cli_fetcher=lambda: "2.1.238",
        )
        args.update(kw)
        return healthcmd.collect_report(**args)

    def render(self, **kw):
        return healthcmd.compose_health_report(self.collect(**kw))


# ---------------------------------------------------------------------------
# zero issues -> collapsed short form
# ---------------------------------------------------------------------------

def test_all_normal_collapses_to_short_form(tmp_path):
    text, ctas = Estate(tmp_path).render()
    lines = text.splitlines()
    assert lines[0] == "✅ 전부 정상"
    assert ctas == []
    assert "관측자 최종 확인 21:28" in text
    # short form: verdict + self + estate/jobs line, then only the fold
    body = text.split("fold::")[0].rstrip().splitlines()
    assert len(body) == 3
    assert "에스테이트 2기 전부 정상" in text
    # counts are computed from the fixture state, not constants
    assert "스케줄 작업 68개 전부 정상" in text


def test_all_normal_uses_registry_display_names(tmp_path):
    text, _ = Estate(tmp_path).render()
    assert "Kim Metal" in text            # registry short name (pre-parenthesis)
    assert "(Metal Kim)" not in text      # long form trimmed


# ---------------------------------------------------------------------------
# issues -> issue block on top, ordering, tones
# ---------------------------------------------------------------------------

def test_single_issue_renders_on_top_with_mtime_downgraded_tone(tmp_path):
    est = Estate(tmp_path)
    # Kojeni's .env edited AFTER the process start -- no snapshot exists, so
    # the mtime fallback must fire with the tone-downgraded copy.
    import os
    env = est.kojeni / ".telegram_bot" / ".env"
    ts = (LSTART + timedelta(hours=1)).timestamp()
    os.utime(env, (ts, ts))
    text, ctas = est.render()
    lines = text.splitlines()
    assert lines[0].startswith("⚠️ 확인 필요 1건")
    assert "Kojeni-D" in lines[0]
    # downgraded tone: "check needed" wording, never the assertive snapshot copy
    assert "재기동 이력이 없어요" in text
    assert "아직 옛 설정으로 돌고" not in text
    assert ctas and ctas[0] == "Kojeni-D 재기동해줘"
    # issue instance sorts ahead of the healthy one in the estate block
    assert text.index("· Kojeni-D") < text.index("· DigEar")
    assert "1기 정상 · 1기 확인 필요" in text


def test_snapshot_sha_drift_uses_assertive_copy(tmp_path):
    est = Estate(tmp_path)
    _write_snapshot(est.kojeni, est.pids["kojeni"], env_sha_ok=False)
    text, ctas = est.render()
    assert text.splitlines()[0].startswith("⚠️ 확인 필요 1건")
    assert "아직 옛 설정으로 돌고" in text     # assertive (primary detection)
    assert "재기동 이력이 없어요" not in text  # not the fallback tone
    assert ctas[0] == "Kojeni-D 재기동해줘"
    assert "스냅샷 sha 불일치" in text          # fold tech line


def test_snapshot_fw_drift_reports_update_not_consumed(tmp_path):
    est = Estate(tmp_path)
    _write_snapshot(est.kojeni, est.pids["kojeni"], snap_fw="1.39.2")
    text, _ = est.render()
    assert "업데이트(v1.39.3)는 받았는데 아직 v1.39.2로 돌고 있어요" in text


def test_stale_snapshot_pid_falls_back_to_mtime(tmp_path):
    est = Estate(tmp_path)
    # snapshot describes a DEAD pid -> must be discarded (crash-restart cover)
    _write_snapshot(est.kojeni, 99999, env_sha_ok=False)
    text, _ = est.render()
    # the bogus sha in the stale snapshot must NOT produce the assertive warn
    assert "아직 옛 설정으로 돌고" not in text
    assert "stale-pid" in text  # fold notes the fallback reason


def test_bridge_down_is_an_issue(tmp_path):
    est = Estate(tmp_path)
    text, ctas = est.render(
        launchctl_list_output=est.launchctl_output(down=("digear",)),
        ps_lstart_output=est.ps_output(down=("digear",)))
    assert text.splitlines()[0].startswith("⚠️")
    assert "DigEar 브릿지가 지금 떠 있지 않아요" in text
    assert "DigEar 재기동해줘" in ctas


# ---------------------------------------------------------------------------
# observer (the observer's observer -- core failure mode)
# ---------------------------------------------------------------------------

def test_missing_jobs_state_surfaces_observer_down(tmp_path):
    est = Estate(tmp_path, jobs_state=None)  # never written
    text, ctas = est.render()
    assert text.splitlines()[0].startswith("⚠️")   # never the all-normal form
    assert "관측자가 안 돌고 있어요" in text
    assert "상태 파일 없음" in text
    assert "헬스 관측자 상태 봐줘" in ctas


def test_stale_heartbeat_never_vouches_all_normal(tmp_path):
    est = Estate(tmp_path)
    state = _healthy_jobs_state()
    state["heartbeat_at"] = (NOW - timedelta(hours=2)).isoformat()
    est.write_jobs(state)
    text, _ = est.render()
    # zero warn records + dead observer must NOT read as "all normal"
    assert "전부 정상" not in text.splitlines()[0]
    assert "잡 관측자 침묵" in text
    assert "19:28" in text  # last heartbeat shown


def test_observer_warn_records_are_listed_and_info_counted(tmp_path):
    est = Estate(tmp_path)
    state = _healthy_jobs_state()
    state["warnings"] = [
        {"kind": "reboot_nonsurvivor", "label": PREFIX + "Kojeni.jpy-vix-watch",
         "level": "warn", "detail": "x"},
        {"kind": "not_loaded", "label": PREFIX + "ag.generic-brief",
         "level": "info", "detail": "y"},
    ]
    est.write_jobs(state)
    text, _ = est.render()
    assert "재부팅하면 사라져요" in text
    assert "참고 항목 1건" in text          # info counted, not listed
    assert "generic-brief" not in text.split("fold::")[0]  # info only in fold


def test_job_warn_instance_part_resolves_to_display_name(tmp_path):
    # A warn on a bridge-instance label (e.g. plist_divergence on the metal
    # bridge itself, the live 2026-08-21 finding) must show the registry
    # display name, not the raw slug.
    est = Estate(tmp_path)
    state = _healthy_jobs_state()
    state["warnings"] = [
        {"kind": "plist_divergence", "label": PREFIX + "metal",
         "level": "warn", "detail": "contents differ"},
    ]
    est.write_jobs(state)
    text, _ = est.render()
    assert "Kim Metal 작업" in text or "Kim Metal --" in text or "Kim Metal " in text
    body = text.split("fold::")[0]
    assert "metal 작업" not in body  # raw slug must not surface in the body


def test_ongoing_fail_streak_from_state_is_shown(tmp_path):
    est = Estate(tmp_path)
    state = _healthy_jobs_state()
    state["jobs"] = {
        PREFIX + "metal.product-health": {
            "bucket": "scheduled", "runs": 10, "last_exit": 1,
            "fail_streak": 3, "notified_at": NOW.isoformat(),
        },
    }
    est.write_jobs(state)
    text, _ = est.render()
    assert "연속 3회 실패" in text


# ---------------------------------------------------------------------------
# engine versions (F8: per-kit, never collapsed)
# ---------------------------------------------------------------------------

def test_engine_versions_preserved_per_kit(tmp_path):
    est = Estate(tmp_path)
    report = est.collect()
    kojeni = [i for i in report["estate"] if i["display"] == "Kojeni-D"][0]
    assert kojeni["engine_versions"] == {"lifekit": 29, "portfolio": 3}
    text, _ = healthcmd.compose_health_report(report)
    assert "lifekit v29" in text and "portfolio v3" in text
    # empty map -> engine segment omitted entirely for that instance
    digear_line = [ln for ln in text.splitlines() if "DigEar" in ln][0]
    assert "엔진" not in digear_line


# ---------------------------------------------------------------------------
# CLI freshness
# ---------------------------------------------------------------------------

def test_live_lookup_failure_is_reported_honestly(tmp_path):
    def boom():
        raise OSError("network down")
    text, _ = Estate(tmp_path).render(latest_cli_fetcher=boom)
    assert "조회 실패" in text


def test_non_native_cli_is_a_warn_with_cta(tmp_path):
    est = Estate(tmp_path)
    text, ctas = est.render(cli_info={
        "resolved_path": "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js",
        "version": "2.0.0", "install_method": "global"})
    assert text.splitlines()[0].startswith("⚠️")
    assert "자동 업데이트가 안 돼요" in text
    assert "CLI 네이티브로 이관해줘" in ctas


def test_install_choice_last_record_is_cross_check_only(tmp_path):
    est = Estate(tmp_path)
    est.choice_path.write_text(
        "classify=global\nresolved_path=/old\ndecision=migrate\nat=2026-08-20T00:00:00Z\n"
        "\n"
        "classify=native\nresolved_path=/u/.local/bin/claude\ndecision=keep\nat=2026-08-21T00:00:00Z\n",
        encoding="utf-8")
    text, ctas = est.render()
    # live judgment (native) wins -> no warn from the recorded history
    assert ctas == []
    assert "classify=native decision=keep" in text  # last record, in fold


def test_parse_install_choice_returns_last_record():
    rec = healthcmd.parse_install_choice(
        "classify=global\ndecision=keep\n\nclassify=native\ndecision=keep\n")
    assert rec == {"classify": "native", "decision": "keep"}
    assert healthcmd.parse_install_choice("") is None


# ---------------------------------------------------------------------------
# read-only guarantee
# ---------------------------------------------------------------------------

def _tree_signature(root: Path):
    return sorted(
        (str(p), p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*") if p.is_file())


def test_collection_writes_nothing_into_other_instances(tmp_path):
    est = Estate(tmp_path)
    before = {n: _tree_signature(r) for n, r in
              (("kojeni", est.kojeni), ("digear", est.digear), ("la", est.la))}
    est.collect()
    after = {n: _tree_signature(r) for n, r in
             (("kojeni", est.kojeni), ("digear", est.digear), ("la", est.la))}
    assert before == after
    # explicitly: no WAL/SHM side files appeared next to any engine db (R5)
    assert not list(est.kojeni.rglob("*.db-wal"))
    assert not list(est.kojeni.rglob("*.db-shm"))


# ---------------------------------------------------------------------------
# display-name fallback
# ---------------------------------------------------------------------------

def test_unregistered_instance_falls_back_to_real_dir_name(tmp_path):
    text, _ = Estate(tmp_path).render()
    assert "DigEar" in text                       # real dir name
    assert "com.telegram-skill-bot.digear" not in text.split("fold::")[0]


# ---------------------------------------------------------------------------
# bot wiring: menu lock (11, health before help) + session non-involvement
# ---------------------------------------------------------------------------

def test_menu_has_eleven_commands_health_before_help():
    # DGN-986 integration merge: authsync (D1's original anchor) was retired
    # from the menu by DGN-1050 after D1 was decided -- only "health
    # immediately before help" still holds. See test_dgn919_command_menu_spec.py.
    from bridge.bot import COMMAND_MENU_SPEC
    names = [c for c, _ in COMMAND_MENU_SPEC]
    assert len(names) == 11
    assert names.index("help") == names.index("health") + 1


def test_health_handler_registered():
    from bridge.bot import TelegramBot
    src = inspect.getsource(TelegramBot._setup_handlers)
    assert 'CommandHandler("health", self._cmd_health)' in src


def test_health_handler_no_session_no_model_and_threaded():
    from bridge.bot import TelegramBot
    src = inspect.getsource(TelegramBot._cmd_health)
    # session non-involvement: no model call, no session manager access
    assert "process_message" not in src
    assert "session_manager" not in src
    # R6: collection must run off the event loop
    assert "asyncio.to_thread" in src
