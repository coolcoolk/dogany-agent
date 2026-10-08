#!/usr/bin/env python3
"""cli-updater.py -- nightly external CLI updater (DGN-1673, owner dec-207).

Runs as the PRE-STEP of the machine-single dawn memory queue
(routines/dawn-memory-queue.sh), before any per-instance consolidate:

    CLI update -> smoke -> model availability probe -> (queue) consolidate

What it does, per locked design (DGN-1673 ticket + dec-163..167):

  claude (native install: <activator> -> <versions>/<ver>)
    * installs the newest release that was published >= 24h ago (cooldown;
      the vendor "stable" channel is rejected -- it lagged a model release).
      Never downgrades. Unpublished versions (listed in the registry "time"
      map but not in "versions") are never picked.
    * keeps the previous version file as rollback material (plus a hardlink
      under <state>/keep/ in case the vendor installer prunes it).
    * smoke = ONE opus turn, prompt exactly "just answer me hi" (dec-165),
      model id read from the response stream. Smoke fail -> atomic symlink
      restore to the previous version + failure alert.
    * the vendor built-in autoUpdates flag is only READ (dec-164): if it is
      on, two updaters would race, so this job alerts and installs nothing.
      It never writes ~/.claude.json.

  codex (standalone install: <codex_home>/packages/standalone/current ->
    releases/<ver>-<triple>) -- r2, dec-166 "version pin + state snapshot":
    * target = pins.conf codex=<ver> when set (operator hold), else the same
      24h-cooled pick as claude. Installed ONLY by explicit version through
      the vendor standalone installer (install.sh --release <ver>; measured
      2026-09-30: per-version release metadata exists, old releases/ dirs
      are kept, a pinned install removes auto-update-version so the vendor
      latest-channel updater stops following latest). Never `codex update`
      (latest only).
    * before install: snapshot every top-level regular file of <codex_home>
      (sqlite + -wal/-shm, config.toml, version.json, models_cache.json ...)
      EXCEPT auth.json (refresh tokens rotate; restoring an old one logs the
      account out), plus packages/standalone/auto-update-version.
    * smoke = `codex --version` == target AND one minimal exec. Exec failing
      -> rollback: current -> previous release dir (atomic), state files
      restored byte-identical, files created after the snapshot quarantined
      (never deleted), auth.json untouched. Exec INCONCLUSIVE (usage limit /
      rate limit / auth) with a matching --version -> new version kept +
      watch alert "smoke inconclusive: <reason>".
    * skipped (alert) while any codex process runs: the sqlite snapshot must
      not be taken under a writer.

  classification (r2) -- a CLI turn is judged from its OUTPUT TEXT, not rc
    alone (a pipeline was measured returning rc 0 while printing ERROR).
    usage-limit / rate-limit / auth text -> "inconclusive (<reason>)": never
    "not available", never a model change, never a rollback on its own.

  model probe -- claude alias-resolved ids (opus comes free from the smoke;
    the other aliases are probed once per CLI version, cached) and codex
    availability of the table slugs plus the gpt-<gen>-<family> candidates.
    A result that differs from lib/dispatch-model-display.tsv is ALERT ONLY:
    the table is never edited (model changes carry quality/cost impact).

  target set -- derived, not listed: distinct runtimes of
    lib/dispatch-tier-bindings.tsv -> lib/dispatch-runtime-executables.tsv ->
    PATH resolution -> install class from the realpath; plus each instance's
    bridge CLAUDE_CLI_PATH pin. A runtime with no executable row, a missing
    binary, an unclassified install or a bridge pinned to another copy is
    alerted, never updated.

  alerts -- the existing seam (DGN-909 / DGN-1330): durable ledger
    <alert-root>/worklog/_CRON_QUEUE_LEDGER.md + an atomic session-inbox drop
    <alert-root>/.telegram_bot/session-inbox/cli-updater-<ts>.md.

  owner notices (r2, dec-208) -- the ONLY things pushed to the owner, one
    push per run at most, through the existing proactive push path
    (<alert-root>/routines/push.sh --audience owner):
      (a) a new model became available (a claude alias resolves to a model
          whose display is not in dispatch-model-display.tsv, or codex lists
          a gpt-<gen>-<family> slug newer than / outside the table)
      (b) a rollback happened (either CLI)
      (c) a running agent's model changed version within its family
          (per registered instance: the model id its bridge session last
          reported, before vs now -- see check_agents)
    Copy lives in config/i18n (cli_update.* keys), never in code. Pushing
    needs BOTH --owner-push (the queue passes it only on --live) AND the file
    <state>/owner-push-armed; otherwise the would-be text is written to the
    ledger as a draft. Everything else stays leader-queue only.

Modes:
  --mode observe (default)  no install, no API turns; plan + alerts only.
  --mode install            requires the arming file <state>/armed (gates
                            BOTH CLIs), else it runs as observe and says so.
                            The queue passes install only on --live.
  --mode agents             only the running-agent model check (c); with
                            --wait-secs N it first waits (bounded) for every
                            bridge restarted since --since EPOCH to report a
                            turn. The queue runs it after its restart step.

Bridge restarts are NOT done here: long-lived bridges keep the old image until
the queue's post-consolidate step restarts them through self_restart.sh
--trigger auto (idle guard). This job only writes <state>/last-run.env for it.

Exit: 0 ran (alerts may have been written) / 1 an install failed or rolled
      back / 2 usage or internal error. The queue treats every rc as fail-open.

Test seams (env; defaults are the real host paths):
  CLI_UPDATER_CLAUDE_ACTIVATOR   ~/.local/bin/claude
  CLI_UPDATER_CLAUDE_VERSIONS    ~/.local/share/claude/versions
  CLI_UPDATER_CLAUDE_JSON        ~/.claude.json
  CLI_UPDATER_CODEX_HOME         ~/.codex
  CLI_UPDATER_CODEX_ACTIVATOR    ~/.local/bin/codex
  CLI_UPDATER_CODEX_INSTALLER    local installer script (default: fetch
                                 https://chatgpt.com/codex/install.sh)
  CLI_UPDATER_CODEX_BUSY_CMD     "pgrep -x codex" (rc 0 = a codex runs)
  CLI_UPDATER_CLAUDE_PROJECTS    ~/.claude/projects
  CLI_UPDATER_PUSH               push command (default <alert-root>/routines/push.sh)
  CLI_UPDATER_REGISTRY_FIXTURE_DIR  read <dir>/claude.json, <dir>/codex.json
                                 instead of the npm registry
  CLI_UPDATER_NOW                epoch override
  CLI_UPDATER_COOLDOWN_SECS      86400
  CLI_UPDATER_CLAUDE_PROBE_ALIASES  comma list; default = claude-code rows
  CLI_UPDATER_CODEX_PROBE_GENS   comma list of generations; default "6"
  CLI_UPDATER_TIER_TSV / _EXEC_TSV / _DISPLAY_TSV   data file overrides

Python 3.9 compatible (macOS /usr/bin/python3); stdlib only.
"""
import calendar
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "lib")
sys.path.insert(0, LIB)
import model_display  # noqa: E402  (lib/ is the only parser of model ids)

SMOKE_PROMPT = "just answer me hi"   # dec-165, fixed string
SMOKE_MODEL = "opus"                  # dec-165
REGISTRY_URLS = {
    "claude": "https://registry.npmjs.org/@anthropic-ai/claude-code",
    "codex": "https://registry.npmjs.org/@openai/codex",
}
ALERT_REPEAT_SECS = 7 * 86400
SOURCE_LABEL = "cli-updater"
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
CODEX_INSTALLER_URL = "https://chatgpt.com/codex/install.sh"
CODEX_STATE_EXCLUDE = ("auth.json",)   # dec-166: tokens rotate, never restore
CODEX_FAMILY = re.compile(r"^gpt-(\d+(?:\.\d+)?)-([a-z]+)$")
# Seconds a codex apply may need (install 300 + --version 60 + exec 120 +
# restore); it is not started when fewer remain in the run budget.
CODEX_APPLY_RESERVE = 500

# ---------------------------------------------------------------------------
# turn classification -- judged from the output TEXT, never from rc alone
# ---------------------------------------------------------------------------
# Vendor wording, measured: codex "You've hit your usage limit ... try again
# at Oct 4th" (2026-09-30), claude weekly-cap wording kept in lockstep with
# dispatch-detached.sh RE_FABLE_CREDITS_DEATH, auth wording from
# codex-ratelimit.py AUTH_EXPIRED_PATTERNS plus the claude /login prompt.
_INCONCLUSIVE = (
    ("usage limit", re.compile(
        r"hit your (usage )?limit|usage limit|requires usage credits|weekly limit "
        r"reached|reached your .*limit|purchase more credits|insufficient_quota|"
        r"quota exceeded", re.I)),
    ("rate limit", re.compile(
        r"rate[ _-]?limit|too many requests|\b429\b|overloaded", re.I)),
    ("auth", re.compile(
        r"token[_ ]expired|authentication token is expired|not authenticated|"
        r"please log ?in|401 unauthorized|please run /login|invalid api key|"
        r"oauth token has expired|authentication_error|sign in again|"
        r"not logged in", re.I)),
)
_UNAVAILABLE = re.compile(
    r"model\b[^\n]{0,80}\b(not available|not found|does not exist|not supported|"
    r"unsupported)|model_not_found|unknown model|invalid model|"
    r"(does not|doesn't) have access to (the )?model", re.I)
_ERROR_LINE = re.compile(r"^\s*(ERROR\b|error:|Error:)", re.M)
_RETRY_HINT = re.compile(r"try again (at|in|after) ([^\n.;]{1,40})", re.I)


def classify_turn(rc, out, err, need_answer=True):
    """(verdict, reason) for one CLI turn.

    verdict: ok | inconclusive | unavailable | fail. Inconclusive wins over
    everything (a usage-limited account says nothing about the model or the
    binary); rc 0 with an error line or no answer is NOT ok.
    """
    text = "%s\n%s" % (out or "", err or "")
    for label, pat in _INCONCLUSIVE:
        if pat.search(text):
            hint = _RETRY_HINT.search(text)
            return "inconclusive", (label + (", try again %s %s" % hint.groups()
                                             if hint else ""))
    if rc == 124:
        return "fail", "timeout"
    if _UNAVAILABLE.search(text):
        return "unavailable", "model not available"
    if '"is_error": true' in text or '"is_error":true' in text:
        return "fail", "turn reported is_error"
    err_line = _ERROR_LINE.search(text)
    if rc == 0 and not err_line and (not need_answer or (out or "").strip()):
        return "ok", ""
    if err_line:
        line = text[err_line.start(1):].split("\n", 1)[0].strip()
        return "fail", line[:160]
    return "fail", "rc=%d%s" % (rc, "" if (out or "").strip() else ", no answer")


def reduce_claude_stream(out):
    """(text worth classifying, clean_success) from a claude stream-json run.

    The raw stream is NOT classified: every healthy run carries a
    rate_limit_event (status "allowed") and the init event lists skill and
    hook text that can contain any word (measured 2026-09-30). Only the
    final result event, a rejected rate-limit event and non-JSON lines count.
    """
    result, parts = None, []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            parts.append(line)
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("type") == "result":
            result = doc
        elif doc.get("type") == "rate_limit_event":
            info = doc.get("rate_limit_info") or {}
            if isinstance(info, dict) and info.get("status") == "rejected":
                parts.append("rate limit rejected (%s)" % info.get("rateLimitType", "?"))
    if isinstance(result, dict):
        if result.get("is_error") or result.get("subtype") not in (None, "success"):
            parts.append('"is_error": true')
            parts.append(str(result.get("result") or result.get("subtype") or ""))
        elif not parts:
            return "", True
    return "\n".join(parts), False


def env(name, default):
    val = os.environ.get(name, "")
    return val if val else default


def home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def now_epoch():
    val = os.environ.get("CLI_UPDATER_NOW", "")
    return int(val) if val else int(time.time())


def vtuple(ver):
    m = _SEMVER.match(ver or "")
    return tuple(int(x) for x in m.groups()) if m else None


def iso_to_epoch(text):
    text = (text or "").strip()
    if not text:
        return None
    base = text.split(".")[0].rstrip("Z")
    try:
        dt = datetime.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return calendar.timegm(dt.timetuple())


def run_cmd(argv, timeout, cwd=None, env_extra=None):
    """(rc, stdout, stderr); timeout -> rc 124; missing binary -> rc 127."""
    child_env = None
    if env_extra:
        child_env = dict(os.environ)
        child_env.update(env_extra)
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, cwd=cwd, env=child_env,
                              stdin=subprocess.DEVNULL,
                              timeout=timeout, text=True, errors="replace")
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except OSError as e:
        return 127, "", str(e)


def read_tsv(path, min_cols):
    rows = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= min_cols:
                    rows.append(cols)
    except OSError:
        pass
    return rows


# ---------------------------------------------------------------------------
# registry (cooldown pick)
# ---------------------------------------------------------------------------

def load_registry(name):
    """Packument dict, or raise RuntimeError. Never substitutes 'latest'."""
    fixture_dir = os.environ.get("CLI_UPDATER_REGISTRY_FIXTURE_DIR", "")
    try:
        if fixture_dir:
            with open(os.path.join(fixture_dir, name + ".json")) as fh:
                return json.load(fh)
        req = urllib.request.Request(REGISTRY_URLS[name],
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # network, parse, missing fixture: all one class
        raise RuntimeError("registry lookup failed for %s: %s" % (name, e))


def pick_cooled(packument, now, cooldown):
    """Newest stable x.y.z published at least `cooldown` seconds ago.

    Returns (version, published_epoch) or (None, None). A version listed in
    "time" but absent from "versions" was unpublished and is never picked.
    """
    times = packument.get("time") or {}
    listed = packument.get("versions")
    best = None
    for ver, stamp in times.items():
        vt = vtuple(ver)
        if vt is None:
            continue
        if isinstance(listed, dict) and ver not in listed:
            continue
        pub = iso_to_epoch(stamp)
        if pub is None or now - pub < cooldown:
            continue
        if best is None or vt > best[0]:
            best = (vt, ver, pub)
    return (best[1], best[2]) if best else (None, None)


# ---------------------------------------------------------------------------
# alerts (existing seam: durable ledger + session-inbox; no push)
# ---------------------------------------------------------------------------

class Alerts(object):
    def __init__(self, state_dir, alert_root, enabled):
        self.state_dir = state_dir
        self.alert_root = alert_root
        self.enabled = enabled
        self.items = []   # (grade, text, dedupe)

    def add(self, grade, text, dedupe=True):
        self.items.append((grade, text, dedupe))

    def _seen_path(self):
        return os.path.join(self.state_dir, "alerts-seen.json")

    def flush(self, now):
        """Returns the list of lines actually delivered (after dedupe)."""
        if not self.items:
            return []
        try:
            with open(self._seen_path()) as fh:
                seen = json.load(fh)
        except (OSError, ValueError):
            seen = {}
        out = []
        for grade, text, dedupe in self.items:
            fp = hashlib.sha1(("%s|%s" % (grade, text)).encode()).hexdigest()
            if dedupe and now - int(seen.get(fp, 0)) < ALERT_REPEAT_SECS:
                continue
            seen[fp] = now
            out.append("- [%s] %s" % (grade, text))
        if not out:
            return []
        if not self.enabled or not self.alert_root:
            for line in out:
                print("[cli-updater] ALERT (not delivered: no alert root): %s" % line)
            return out
        self._write_seam(out)
        tmp = self._seen_path() + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as fh:
            json.dump(seen, fh, indent=1, sort_keys=True)
        os.replace(tmp, self._seen_path())
        return out

    def _write_seam(self, lines):
        root = self.alert_root
        ts_human = time.strftime("%Y-%m-%d %H:%M")
        ts_file = time.strftime("%Y%m%d-%H%M%S")
        ledger = os.path.join(root, "worklog", "_CRON_QUEUE_LEDGER.md")
        os.makedirs(os.path.dirname(ledger), exist_ok=True)
        if not os.path.exists(ledger):
            with open(ledger, "w") as fh:
                fh.write("# _CRON_QUEUE_LEDGER -- cron findings ledger (DGN-909)\n")
        with open(ledger, "a") as fh:
            fh.write("\n## %s -- %s (%d)\n" % (ts_human, SOURCE_LABEL, len(lines)))
            fh.write("\n".join(lines) + "\n")
        inbox = os.path.join(root, ".telegram_bot", "session-inbox")
        os.makedirs(inbox, exist_ok=True)
        body = [
            "[nightly CLI updater -- triage (DGN-1673)]",
            "",
            "The dawn-queue pre-step (routines/cli-updater.py) reports %d item(s)."
            % len(lines),
            "Assess them and tell the owner only what needs a decision; the job "
            "never edits dispatch-model-display.tsv and never flips the vendor "
            "autoUpdates flag. Owner-grade events (new model / rollback / agent "
            "model change) are pushed by the job itself once owner-push-armed "
            "exists -- do not relay those a second time. If nothing needs the "
            "owner, end with NO_PUSH.",
            "Durable copy: worklog/_CRON_QUEUE_LEDGER.md (%s entry)." % ts_human,
            "",
        ] + lines
        name = "%s-%s-%d.md" % (SOURCE_LABEL, ts_file, os.getpid())
        tmp = os.path.join(inbox, ".%s.tmp" % name)
        with open(tmp, "w") as fh:
            fh.write("\n".join(body) + "\n")
        os.replace(tmp, os.path.join(inbox, name))


# ---------------------------------------------------------------------------
# target derivation
# ---------------------------------------------------------------------------

def read_env_value(path, key):
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def classify(realpath, cfg):
    versions = os.path.realpath(cfg["claude_versions"])
    releases = os.path.realpath(os.path.join(cfg["codex_home"], "packages",
                                             "standalone", "releases"))
    if os.path.dirname(realpath) == versions:
        return "claude-native"
    if realpath.startswith(releases + os.sep):
        return "codex-standalone"
    return "other"


def derive_targets(cfg, alerts, instance_roots):
    runtimes = []
    for cols in read_tsv(cfg["tier_tsv"], 3):
        if cols[0] not in runtimes:
            runtimes.append(cols[0])
    exe_of = {}
    for cols in read_tsv(cfg["exec_tsv"], 2):
        exe_of[cols[0]] = cols[1]
    targets = {}
    for rt in runtimes:
        exe = exe_of.get(rt)
        if not exe:
            alerts.add("watch", "runtime '%s' is in dispatch-tier-bindings.tsv but has "
                       "no row in dispatch-runtime-executables.tsv -- its CLI is "
                       "not watched" % rt)
            continue
        t = targets.setdefault(exe, {"executable": exe, "runtimes": [],
                                     "consumers": []})
        t["runtimes"].append(rt)
    for exe, t in sorted(targets.items()):
        resolved = shutil.which(exe) or ""
        t["resolved"] = resolved
        if not resolved:
            t["class"] = "missing"
            alerts.add("watch", "executable '%s' (runtimes %s) not found on PATH"
                       % (exe, ",".join(t["runtimes"])))
            continue
        t["realpath"] = os.path.realpath(resolved)
        t["class"] = classify(t["realpath"], cfg)
        if t["class"] == "other":
            alerts.add("watch", "'%s' resolves to %s -- unclassified install, "
                       "observed only (never auto-updated)" % (exe, t["realpath"]))
        elif (t["class"] == "claude-native"
              and os.path.realpath(cfg["claude_activator"]) != t["realpath"]):
            alerts.add("watch", "'claude' on PATH (%s) is not the native activator "
                       "%s" % (resolved, cfg["claude_activator"]))
    # bridge CLI resolution: explicit CLAUDE_CLI_PATH wins over PATH
    claude_real = os.path.realpath(cfg["claude_activator"])
    for root in instance_roots:
        for envf in (os.path.join(root, ".telegram_bot", ".env"),
                     os.path.join(root, "bridge", ".env")):
            pin = read_env_value(envf, "CLAUDE_CLI_PATH")
            if not pin:
                continue
            real = os.path.realpath(os.path.expanduser(pin))
            consumer = {"consumer": "bridge:%s" % os.path.basename(root.rstrip("/")),
                        "pin": pin, "realpath": real, "file": envf}
            targets.setdefault("claude", {"executable": "claude", "runtimes": [],
                                          "consumers": []})["consumers"].append(consumer)
            if real != claude_real:
                alerts.add("watch", "bridge %s pins CLAUDE_CLI_PATH=%s (%s), not the "
                           "native activator -- this bridge is not updated by the "
                           "nightly job" % (consumer["consumer"], pin, real))
            break
    return targets


# ---------------------------------------------------------------------------
# claude native
# ---------------------------------------------------------------------------

def claude_installed(cfg):
    try:
        target = os.readlink(cfg["claude_activator"])
    except OSError:
        return None, None
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(cfg["claude_activator"]), target)
    return target, os.path.basename(target)


def claude_autoupdates(cfg):
    try:
        with open(cfg["claude_json"]) as fh:
            val = json.load(fh).get("autoUpdates")
        return val
    except (OSError, ValueError, AttributeError):
        return None


def swap_symlink(link, target):
    """Atomic activator swap: symlink to a temp name, rename over the link."""
    tmp = "%s.cli-updater.%d" % (link, os.getpid())
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def version_of(binary):
    rc, out, _ = run_cmd([binary, "--version"], 60)
    if rc != 0:
        return ""
    tok = (out.strip().split() or [""])[0]
    return tok


def claude_turn(activator, alias, workdir, timeout):
    """One minimal turn. Returns (verdict, reason, model_id, rc).

    verdict per classify_turn over the REDUCED stream (reduce_claude_stream):
    ok needs rc 0, a clean result event and a reported claude-* model id.
    """
    rc, out, err = run_cmd([activator, "-p", SMOKE_PROMPT, "--model", alias,
                            "--output-format", "stream-json", "--verbose"],
                           timeout, cwd=workdir)
    fd, path = tempfile.mkstemp(prefix="cli-updater-stream.", dir=workdir)
    with os.fdopen(fd, "w") as fh:
        fh.write(out)
    ids = model_display.observe_from_stream(path)
    os.unlink(path)
    model_id = ids[0] if ids else ""
    text, clean = reduce_claude_stream(out)
    verdict, reason = classify_turn(rc, text, err, need_answer=False)
    if verdict == "ok" and not (clean and model_id.startswith("claude-")):
        verdict, reason = "fail", ("no clean result event" if not clean
                                   else "no model id reported")
    if verdict != "ok":
        model_id = ""   # an id from a failed turn is never evidence
    return verdict, reason, model_id, rc


def keep_copy(cfg, old_file, old_ver):
    keep = os.path.join(cfg["state_dir"], "keep", "claude")
    os.makedirs(keep, exist_ok=True)
    for name in os.listdir(keep):
        if name != old_ver:
            os.unlink(os.path.join(keep, name))
    dest = os.path.join(keep, old_ver)
    if os.path.exists(dest):
        return dest
    try:
        os.link(old_file, dest)
        return dest
    except OSError:
        return ""


def rollback_claude(cfg, pre_target, pre_ver, keep):
    """Restore the activator to pre_target. Returns list of proof strings and ok."""
    proof = []
    if not os.path.exists(pre_target) and keep and os.path.exists(keep):
        os.link(keep, pre_target)
        proof.append("previous version file was gone; restored from keep copy")
    if not os.path.exists(pre_target):
        return ["previous version file missing and no keep copy -- cannot roll back"], False
    swap_symlink(cfg["claude_activator"], pre_target)
    cur_target, _ = claude_installed(cfg)
    ok_link = cur_target == pre_target
    proof.append("readlink == previous: %s" % ("yes" if ok_link else "NO (%s)" % cur_target))
    ver = version_of(cfg["claude_activator"])
    ok_ver = ver == pre_ver
    proof.append("--version == %s: %s" % (pre_ver, "yes" if ok_ver else "NO (%s)" % ver))
    return proof, ok_link and ok_ver


def update_claude(cfg, mode, alerts, report, now):
    pre_target, pre_ver = claude_installed(cfg)
    rec = {"activator": cfg["claude_activator"], "pre_target": pre_target,
           "pre_version": pre_ver,
           "on_disk_versions": sorted(os.listdir(cfg["claude_versions"]))
           if os.path.isdir(cfg["claude_versions"]) else []}
    report["claude"] = rec
    if not pre_target:
        alerts.add("watch", "claude activator %s is not a symlink -- native update "
                   "skipped" % cfg["claude_activator"])
        rec["action"] = "skip-no-activator"
        return 0
    auto = claude_autoupdates(cfg)
    rec["vendor_autoupdates"] = auto
    if auto is True:
        alerts.add("skip", "claude built-in autoUpdates is ON in %s -- two updaters "
                   "would race; nightly install skipped (dec-164: this job never "
                   "flips it)" % cfg["claude_json"])
        rec["action"] = "skip-vendor-autoupdate"
        return 0
    try:
        chosen, published = pick_cooled(load_registry("claude"), now, cfg["cooldown"])
    except RuntimeError as e:
        alerts.add("skip", "claude: %s -- no install tonight" % e)
        rec["action"] = "skip-lookup-failed"
        return 0
    rec["chosen_version"], rec["chosen_published"] = chosen, published
    if not chosen or vtuple(pre_ver) is None or vtuple(chosen) <= vtuple(pre_ver):
        rec["action"] = "up-to-date"
        return 0
    if mode != "install":
        alerts.add("watch", "claude %s -> %s is eligible (published >= %dh ago); "
                   "updater in observe mode (%s)" % (pre_ver, chosen,
                                                    cfg["cooldown"] // 3600,
                                                    report["mode_reason"]))
        rec["action"] = "observe-would-install"
        return 0

    keep = keep_copy(cfg, pre_target, pre_ver)
    rec["keep_copy"] = keep
    rc, out, err = run_cmd([cfg["claude_activator"], "install", chosen], 600,
                           cwd=cfg["state_dir"])
    rec["vendor_notes"] = (out + err).strip()[-2000:]
    new_file = os.path.join(cfg["claude_versions"], chosen)
    if rc != 0 or not os.access(new_file, os.X_OK):
        moved = claude_installed(cfg)[0] != pre_target
        proof, ok = rollback_claude(cfg, pre_target, pre_ver, keep)
        rec.update(action="install-failed", install_rc=rc, rollback_ok=ok,
                   rollback_proof=proof)
        alerts.add("FAIL", "claude install %s failed (rc=%d); activator restored to "
                   "%s: %s" % (chosen, rc, pre_ver, "; ".join(proof)), dedupe=False)
        if moved:   # nothing changed = nothing rolled back: leader queue only
            report["owner"].rollback("Claude Code", chosen, pre_ver, ok)
        return 1
    cur_target, _ = claude_installed(cfg)
    if cur_target != new_file:
        swap_symlink(cfg["claude_activator"], new_file)
    if claude_autoupdates(cfg) != auto:
        alerts.add("watch", "claude autoUpdates changed during install (%r -> %r); "
                   "left as is" % (auto, claude_autoupdates(cfg)))

    # smoke: version + one opus turn with the fixed prompt
    ver = version_of(cfg["claude_activator"])
    verdict, why, model_id, trc = claude_turn(cfg["claude_activator"], SMOKE_MODEL,
                                              cfg["state_dir"], cfg["smoke_timeout"])
    smoke = {"version": ver, "rc": trc, "model_id": model_id, "verdict": verdict,
             "reason": why}
    rec["smoke"] = smoke
    if ver != chosen or verdict in ("fail", "unavailable"):
        proof, rok = rollback_claude(cfg, pre_target, pre_ver, keep)
        rec.update(action="smoke-failed-rolled-back", rollback_ok=rok,
                   rollback_proof=proof)
        alerts.add("FAIL", "claude %s -> %s smoke FAILED (version=%s rc=%d model=%s %s); "
                   "rolled back to %s: %s" % (pre_ver, chosen, ver or "?", trc,
                                              model_id or "-", why[-120:], pre_ver,
                                              "; ".join(proof)), dedupe=False)
        report["owner"].rollback("Claude Code", chosen, pre_ver, rok)
        return 1
    if verdict == "inconclusive":
        rec["action"] = "installed"
        alerts.add("watch", "claude %s -> %s installed; --version matches but smoke "
                   "inconclusive: %s -- kept (not a model or binary verdict)"
                   % (pre_ver, chosen, why), dedupe=False)
        return 0
    rec["action"] = "installed"
    probe_cache_put(cfg, chosen, SMOKE_MODEL, model_id)
    alerts.add("ok", "claude %s -> %s installed (published %s UTC), smoke ok: opus = "
               "%s. Previous version kept for rollback. Running bridges keep the old "
               "image until the post-consolidate restart step."
               % (pre_ver, chosen,
                  time.strftime("%Y-%m-%d %H:%M", time.gmtime(published)), model_id),
               dedupe=False)
    return 0


# ---------------------------------------------------------------------------
# model probe
# ---------------------------------------------------------------------------

def probe_cache_path(cfg, version):
    return os.path.join(cfg["state_dir"], "probe-claude-%s.json" % version)


def probe_cache_get(cfg, version):
    try:
        with open(probe_cache_path(cfg, version)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def probe_cache_put(cfg, version, alias, model_id):
    data = probe_cache_get(cfg, version)
    data[alias] = model_id
    tmp = probe_cache_path(cfg, version) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, probe_cache_path(cfg, version))


def over_budget(cfg, reserve=0):
    return time.time() - cfg["started"] > cfg["budget"] - reserve


def codex_models_cache(cfg):
    """(listed slugs, meta) from <codex_home>/models_cache.json.

    codex itself refreshes this account model list (measured 2026-09-30: it
    was refreshed while the account sat at its usage limit), so it answers
    "is the model offered to this account" without spending a turn -- the
    evidence an exec probe cannot give while the account is limited.
    """
    try:
        with open(os.path.join(cfg["codex_home"], "models_cache.json")) as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return set(), {}
    models = doc.get("models") if isinstance(doc, dict) else None
    listed = set()
    for m in models if isinstance(models, list) else []:
        if (isinstance(m, dict) and isinstance(m.get("slug"), str)
                and m.get("visibility", "list") == "list"):
            listed.add(m["slug"])
    return listed, {"fetched_at": doc.get("fetched_at"),
                    "client_version": doc.get("client_version")}


def probe_models(cfg, mode, alerts, report):
    rows = read_tsv(cfg["display_tsv"], 4)
    shown = set(r[3] for r in rows)
    probe = {"claude": {}, "codex": {}, "codex_listed": [], "inconclusive": {}}
    report["probe"] = probe
    owner = report["owner"]
    live = mode == "install"

    # claude: alias -> resolved id, once per CLI version
    _, cur_ver = claude_installed(cfg)
    aliases = [r[1] for r in rows if r[0] == "claude-code"]
    want = os.environ.get("CLI_UPDATER_CLAUDE_PROBE_ALIASES", "")
    if want:
        aliases = [a for a in aliases if a in want.split(",")]
    cache = probe_cache_get(cfg, cur_ver) if cur_ver else {}
    for alias in aliases:
        mid = cache.get(alias, "")
        if not mid and live and cur_ver and over_budget(cfg):
            alerts.add("probe", "claude alias '%s' probe skipped: run budget %ss spent"
                       % (alias, cfg["budget"]))
        elif not mid and live and cur_ver:
            verdict, why, mid, _rc = claude_turn(cfg["claude_activator"], alias,
                                                 cfg["state_dir"], cfg["smoke_timeout"])
            if verdict == "ok":
                probe_cache_put(cfg, cur_ver, alias, mid)
            elif verdict == "inconclusive":
                probe["inconclusive"]["claude:" + alias] = why
                alerts.add("probe", "claude alias '%s' probe inconclusive (%s) on CLI %s "
                           "-- no verdict, table NOT edited" % (alias, why, cur_ver))
            else:
                alerts.add("probe", "claude alias '%s' probe failed on CLI %s (%s)"
                           % (alias, cur_ver, why))
        probe["claude"][alias] = mid
        if not mid:
            continue
        row = model_display.tsv_row("claude-code", alias, cfg["display_tsv"])
        table = row[1] if row else ""
        got = model_display.parse_model_id(mid) or mid
        # a bare table display ("Claude Opus") pins no version: family must match
        if table and not (got == table or (len(table.split()) == 2
                                           and got.startswith(table + " "))):
            alerts.add("probe", "claude alias '%s' resolves to %s (%s) on CLI %s but "
                       "dispatch-model-display.tsv shows '%s' -- table NOT edited"
                       % (alias, mid, got, cur_ver, table))
        if model_display.parse_model_id(mid) and got not in shown:
            owner.new_model(got)

    # codex: the account model list (no turn spent) + exec probes (live only)
    table = {}
    for r in rows:
        if r[0] != "codex":
            continue
        m = CODEX_FAMILY.match(r[2])
        if m:
            table[r[1]] = (r[2], m.group(1), m.group(2))
    table_slugs = set(v[0] for v in table.values())
    fam_gen = {}
    for slug, gen, fam in table.values():
        fam_gen[fam] = max(fam_gen.get(fam, 0.0), float(gen))
    listed, meta = codex_models_cache(cfg)
    probe["codex_listed"] = sorted(listed)
    probe["codex_listed_meta"] = meta
    codex = shutil.which("codex")
    verdicts = probe["codex"]
    if live and codex:
        gens = env("CLI_UPDATER_CODEX_PROBE_GENS", "6").split(",")
        cands = []
        for alias, (slug, _gen, fam) in sorted(table.items()):
            for c in [slug] + ["gpt-%s-%s" % (g.strip(), fam) for g in gens if g.strip()]:
                if c not in cands:
                    cands.append(c)
        for c in cands:
            if over_budget(cfg):
                alerts.add("probe", "codex probe of '%s' skipped: run budget %ss spent"
                           % (c, cfg["budget"]))
                continue
            rc, out, err = run_cmd([codex, "exec", "--skip-git-repo-check", "--ephemeral",
                                    "-s", "read-only", "-m", c, SMOKE_PROMPT], 120,
                                   cwd=cfg["state_dir"])
            verdict, why = classify_turn(rc, out, err)
            verdicts[c] = verdict
            if verdict == "inconclusive":
                probe["inconclusive"]["codex:" + c] = why
    reasons = probe["inconclusive"]
    for alias, (slug, gen, fam) in sorted(table.items()):
        v = verdicts.get(slug)
        if v == "unavailable" and slug not in listed:
            alerts.add("probe", "codex '%s' (table slug for %s) is NOT available -- "
                       "table NOT edited" % (slug, alias))
        elif v == "inconclusive" and slug not in listed:
            alerts.add("probe", "codex '%s' (table slug for %s) probe inconclusive (%s) "
                       "-- no verdict, table NOT edited"
                       % (slug, alias, reasons.get("codex:" + slug, "?")))
        elif v == "fail" and slug not in listed:
            alerts.add("probe", "codex '%s' (table slug for %s) probe failed -- no "
                       "verdict, table NOT edited" % (slug, alias))
    available = set(c for c, v in verdicts.items() if v == "ok") | listed
    for c in sorted(available):
        m = CODEX_FAMILY.match(c)
        if not m or c in table_slugs:
            continue
        gen, fam = float(m.group(1)), m.group(2)
        if fam in fam_gen and gen <= fam_gen[fam]:
            continue   # an older generation of a family the table already runs
        src = "offered to the account" if c in listed else "answered a probe"
        if fam in fam_gen:
            alias = [a for a, v in sorted(table.items()) if v[2] == fam][0]
            alerts.add("probe", "codex '%s' is available; table has '%s' for %s (%s) "
                       "-- table NOT edited" % (c, table[alias][0], alias, src))
        else:
            alerts.add("probe", "codex '%s' is available (%s): a family the table does "
                       "not have -- table NOT edited" % (c, src))
        owner.new_model(model_display.parse_model_id(c) or c)


# ---------------------------------------------------------------------------
# codex standalone: pinned install + state snapshot (dec-166)
# ---------------------------------------------------------------------------

def sha256_of(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def codex_base(cfg):
    return os.path.join(cfg["codex_home"], "packages", "standalone")


def codex_installed(cfg):
    """(raw readlink of current, version from current/codex-package.json)."""
    cur = os.path.join(codex_base(cfg), "current")
    try:
        target = os.readlink(cur)
    except OSError:
        return None, None
    try:
        with open(os.path.join(cur, "codex-package.json")) as fh:
            return target, json.load(fh).get("version", "")
    except (OSError, ValueError):
        return target, None


def codex_version_of(binary):
    """Last token of `codex --version` ("codex-cli 0.156.1"), as the vendor
    installer's own version_from_binary reads it."""
    rc, out, _ = run_cmd([binary, "--version"], 60)
    if rc != 0:
        return ""
    first = (out.strip().splitlines() or [""])[0].split()
    return first[-1] if first else ""


def codex_state_names(cfg):
    names = []
    for name in sorted(os.listdir(cfg["codex_home"])):
        p = os.path.join(cfg["codex_home"], name)
        if name in CODEX_STATE_EXCLUDE or os.path.islink(p) or not os.path.isfile(p):
            continue
        names.append(name)
    return names


def snapshot_codex_state(cfg, snap):
    """Copy every top-level state file except auth.json (+ auto-update-version).

    Returns the manifest {"files": {name: sha256}, "auto_update_version": sha
    or None}. One snapshot is kept (the previous one is replaced).
    """
    if os.path.isdir(snap):
        shutil.rmtree(snap)
    files_dir = os.path.join(snap, "files")
    os.makedirs(files_dir)
    manifest = {"files": {}, "auto_update_version": None}
    for name in codex_state_names(cfg):
        src = os.path.join(cfg["codex_home"], name)
        shutil.copy2(src, os.path.join(files_dir, name))
        manifest["files"][name] = sha256_of(os.path.join(files_dir, name))
    auv = os.path.join(codex_base(cfg), "auto-update-version")
    if os.path.isfile(auv):
        shutil.copy2(auv, os.path.join(snap, "auto-update-version"))
        manifest["auto_update_version"] = sha256_of(auv)
    with open(os.path.join(snap, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    return manifest


def restore_codex_state(cfg, snap, manifest):
    """Put the snapshot back byte-identical. Files created after the snapshot
    are quarantined under <snap>/post-install-extra (never deleted). auth.json
    is never read, moved or written. Returns (ok, proof list)."""
    home_dir = cfg["codex_home"]
    files_dir = os.path.join(snap, "files")
    extra = os.path.join(snap, "post-install-extra")
    moved = []
    for name in codex_state_names(cfg):
        if name not in manifest["files"]:
            os.makedirs(extra, exist_ok=True)
            shutil.move(os.path.join(home_dir, name), os.path.join(extra, name))
            moved.append(name)
    bad = []
    for name, sha in sorted(manifest["files"].items()):
        tmp = os.path.join(home_dir, ".%s.cli-updater-restore" % name)
        shutil.copy2(os.path.join(files_dir, name), tmp)
        os.replace(tmp, os.path.join(home_dir, name))
        if sha256_of(os.path.join(home_dir, name)) != sha:
            bad.append(name)
    auv = os.path.join(codex_base(cfg), "auto-update-version")
    if manifest.get("auto_update_version"):
        tmp = auv + ".cli-updater-restore"
        shutil.copy2(os.path.join(snap, "auto-update-version"), tmp)
        os.replace(tmp, auv)
        if sha256_of(auv) != manifest["auto_update_version"]:
            bad.append("auto-update-version")
    elif os.path.exists(auv):
        os.makedirs(extra, exist_ok=True)
        shutil.move(auv, os.path.join(extra, "auto-update-version"))
    proof = ["state files restored byte-identical: %s" % (
        "yes (%d)" % len(manifest["files"]) if not bad else "NO (%s)" % ",".join(bad))]
    if moved:
        proof.append("files created after the snapshot quarantined: %s" % ",".join(moved))
    return not bad, proof


def codex_installer(cfg, rec):
    """Path of the vendor standalone installer, or raise RuntimeError."""
    local = os.environ.get("CLI_UPDATER_CODEX_INSTALLER", "")
    if local:
        path = local
    else:
        path = os.path.join(cfg["state_dir"], "codex-install.sh")
        try:
            # The vendor edge answers 403 to the default Python-urllib
            # User-Agent (measured 2026-09-30: urllib default 403, named UA 200).
            req = urllib.request.Request(
                CODEX_INSTALLER_URL, headers={"User-Agent": "dogany-cli-updater/1"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
        except Exception as e:
            raise RuntimeError("codex installer fetch failed: %s" % e)
        if not body.startswith(b"#!"):
            raise RuntimeError("codex installer fetch returned a non-script body")
        with open(path + ".tmp", "wb") as fh:
            fh.write(body)
        os.replace(path + ".tmp", path)
    rec["installer_sha256"] = sha256_of(path)
    return path


def rollback_codex(cfg, pre_target, pre_ver, act_pre, snap, manifest, auth_pre):
    """current -> previous release dir, activator link back, state restored.
    Returns (proof list, ok)."""
    proof = []
    cur = os.path.join(codex_base(cfg), "current")
    if not os.path.isdir(pre_target):
        return ["previous release dir %s missing -- cannot roll back"
                % os.path.basename(pre_target)], False
    swap_symlink(cur, pre_target)
    act = cfg["codex_activator"]
    if act_pre:
        try:
            now_act = os.readlink(act)
        except OSError:
            now_act = ""
        if now_act != act_pre:
            swap_symlink(act, act_pre)
    ok_link = os.readlink(cur) == pre_target
    proof.append("current == previous: %s" % ("yes" if ok_link else "NO"))
    _, pkg_ver = codex_installed(cfg)
    ver = codex_version_of(act)
    ok_ver = pkg_ver == pre_ver and ver == pre_ver
    proof.append("--version == %s: %s" % (pre_ver, "yes" if ok_ver else
                                         "NO (package %s, binary %s)" % (pkg_ver, ver)))
    ok_state, sproof = restore_codex_state(cfg, snap, manifest)
    proof.extend(sproof)
    auth_now = sha256_of(os.path.join(cfg["codex_home"], "auth.json"))
    proof.append("auth.json untouched: %s" % ("yes" if auth_now == auth_pre else "CHANGED"))
    return proof, ok_link and ok_ver and ok_state


def update_codex(cfg, mode, alerts, report, now):
    base = codex_base(cfg)
    rec = {}
    report["codex"] = rec
    pre_target, installed = codex_installed(cfg)
    if not installed:
        return 0
    rec.update(installed=installed, pre_target=pre_target)
    try:
        rec["on_disk_releases"] = sorted(os.listdir(os.path.join(base, "releases")))
        rec["state_fp"] = sorted(n for n in os.listdir(cfg["codex_home"])
                                 if n.endswith(".sqlite"))
    except OSError:
        pass
    pin = read_env_value(os.path.join(cfg["state_dir"], "pins.conf"), "codex")
    rec["pin"] = pin
    if pin:
        target = pin
    else:
        try:
            target, _ = pick_cooled(load_registry("codex"), now, cfg["cooldown"])
        except RuntimeError as e:
            alerts.add("skip", "codex: %s -- no install tonight" % e)
            rec["action"] = "skip-lookup-failed"
            return 0
        rec["eligible"] = target
    rec["target"] = target
    if (not target or target == installed or vtuple(target) is None
            or (not pin and (vtuple(installed) is None
                             or vtuple(target) <= vtuple(installed)))):
        rec["action"] = "up-to-date"
        return 0
    why_target = "pin" if pin else "published >= %dh ago" % (cfg["cooldown"] // 3600)
    if mode != "install":
        alerts.add("watch", "codex %s -> %s is eligible (%s); updater in observe mode "
                   "(%s)" % (installed, target, why_target, report["mode_reason"]))
        rec["action"] = "observe-would-install"
        return 0
    if over_budget(cfg, CODEX_APPLY_RESERVE):
        alerts.add("skip", "codex %s -> %s skipped tonight: less than %ss of the run "
                   "budget left" % (installed, target, CODEX_APPLY_RESERVE))
        rec["action"] = "skip-budget"
        return 0
    busy_rc, busy_out, _ = run_cmd(env("CLI_UPDATER_CODEX_BUSY_CMD",
                                       "pgrep -x codex").split(), 30)
    if busy_rc == 0:
        alerts.add("skip", "codex %s -> %s skipped tonight: a codex process is running "
                   "(pid %s) -- the state snapshot needs no writer"
                   % (installed, target, " ".join(busy_out.split()[:5])))
        rec["action"] = "skip-busy"
        return 0
    rel_name = os.path.basename(pre_target.rstrip("/"))
    if not rel_name.startswith(installed + "-"):
        alerts.add("watch", "codex current -> %s does not look like <ver>-<triple>; "
                   "not auto-updated" % rel_name)
        rec["action"] = "skip-unclassified"
        return 0
    triple = rel_name[len(installed) + 1:]
    new_dir = os.path.join(base, "releases", "%s-%s" % (target, triple))
    act = cfg["codex_activator"]
    try:
        act_pre = os.readlink(act)
    except OSError:
        act_pre = ""
    auth_pre = sha256_of(os.path.join(cfg["codex_home"], "auth.json"))
    snap = os.path.join(cfg["state_dir"], "codex-state-snapshot")
    manifest = snapshot_codex_state(cfg, snap)
    rec["snapshot"] = {"dir": snap, "files": len(manifest["files"])}
    try:
        installer = codex_installer(cfg, rec)
    except RuntimeError as e:
        alerts.add("skip", "codex %s -> %s: %s -- no install tonight" % (installed, target, e))
        rec["action"] = "skip-installer"
        return 0
    bin_dir = os.path.dirname(act)
    rc, out, err = run_cmd(["/bin/sh", installer, "--release", target], 300,
                           cwd=cfg["state_dir"], env_extra={
                               "CODEX_NON_INTERACTIVE": "1",
                               "CODEX_HOME": cfg["codex_home"],
                               "CODEX_INSTALL_DIR": bin_dir,
                               # bin dir on PATH: the installer then never
                               # appends a PATH block to a shell profile
                               "PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")})
    rec["vendor_notes"] = (out + err).strip()[-2000:]
    _, pkg_ver = codex_installed(cfg)
    landed = (os.path.realpath(os.path.join(base, "current")) == os.path.realpath(new_dir)
              and pkg_ver == target)
    if rc != 0 or not landed:
        try:
            moved = os.readlink(os.path.join(base, "current")) != pre_target
        except OSError:
            moved = True
        proof, ok = rollback_codex(cfg, pre_target, installed, act_pre, snap, manifest,
                                   auth_pre)
        rec.update(action="install-failed", install_rc=rc, rollback_ok=ok,
                   rollback_proof=proof)
        alerts.add("FAIL", "codex install %s failed (rc=%d, landed=%s); restored %s: %s"
                   % (target, rc, landed, installed, "; ".join(proof)), dedupe=False)
        if moved:
            report["owner"].rollback("Codex", target, installed, ok)
        return 1

    ver = codex_version_of(act)
    src, sout, serr = run_cmd([act, "exec", "--skip-git-repo-check", "--ephemeral",
                               "-s", "read-only", SMOKE_PROMPT], 120, cwd=cfg["state_dir"])
    verdict, why = classify_turn(src, sout, serr)
    rec["smoke"] = {"version": ver, "rc": src, "verdict": verdict, "reason": why}
    auto_after = os.path.exists(os.path.join(base, "auto-update-version"))
    rec["vendor_auto_update_version_present"] = auto_after
    if ver != target or verdict in ("fail", "unavailable"):
        proof, ok = rollback_codex(cfg, pre_target, installed, act_pre, snap, manifest,
                                   auth_pre)
        rec.update(action="smoke-failed-rolled-back", rollback_ok=ok, rollback_proof=proof)
        alerts.add("FAIL", "codex %s -> %s smoke FAILED (version=%s exec=%s %s); rolled "
                   "back to %s: %s" % (installed, target, ver or "?", verdict, why,
                                       installed, "; ".join(proof)), dedupe=False)
        report["owner"].rollback("Codex", target, installed, ok)
        return 1
    rec["action"] = "installed"
    if sha256_of(os.path.join(cfg["codex_home"], "auth.json")) != auth_pre:
        alerts.add("watch", "codex auth.json changed during the %s install (left as is: "
                   "it is never snapshotted or restored)" % target, dedupe=False)
    if verdict == "inconclusive":
        alerts.add("watch", "codex %s -> %s installed; --version matches but smoke "
                   "inconclusive: %s -- kept, previous release %s kept for rollback"
                   % (installed, target, why, rel_name), dedupe=False)
        return 0
    alerts.add("ok", "codex %s -> %s installed (%s), smoke ok. Previous release %s and "
               "a state snapshot kept for rollback." % (installed, target, why_target,
                                                         rel_name), dedupe=False)
    return 0


# ---------------------------------------------------------------------------
# running-agent model check (owner notice c)
# ---------------------------------------------------------------------------
# The bridge persists no resolved model id (last_model.json / sessions.json
# carry the alias only; DGN-1227 model_observed exists for dispatch runs
# only). The observation is the bridge session's own Claude Code transcript:
# sessions.json session_id -> <projects>/<enc(root)>/<sid>.jsonl, whose
# assistant lines carry message.model -- the same field
# model_display.observe_from_stream reads for dispatch streams. Only the
# bridge's session ids are read, so headless `claude -p` runs from the same
# workspace (consolidate, probes) never count as the agent's model.

def _project_dir(cfg, root):
    enc = re.sub(r"[^A-Za-z0-9]", "-", root.rstrip("/"))
    return os.path.join(cfg["claude_projects"], enc)


def bridge_observed_model(cfg, root):
    """(model id, epoch of that turn's transcript write) or ("", 0)."""
    try:
        with open(os.path.join(root, ".telegram_bot", "sessions.json")) as fh:
            sessions = json.load(fh)
    except (OSError, ValueError):
        return "", 0
    sids = []
    for val in sessions.values() if isinstance(sessions, dict) else []:
        if isinstance(val, dict) and isinstance(val.get("session_id"), str):
            sids.append(val["session_id"])
    best = None
    for pdir in {_project_dir(cfg, root), _project_dir(cfg, os.path.realpath(root))}:
        for sid in sids:
            if not re.match(r"^[A-Za-z0-9-]+$", sid):
                continue
            path = os.path.join(pdir, sid + ".jsonl")
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            if best is None or mt > best[0]:
                best = (mt, path)
    if not best:
        return "", 0
    try:
        with open(best[1], "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 512 * 1024))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return "", 0
    for line in reversed(tail.splitlines()):
        if '"assistant"' not in line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        msg = doc.get("message") if isinstance(doc, dict) else None
        mid = msg.get("model") if isinstance(msg, dict) else None
        if doc.get("type") == "assistant" and isinstance(mid, str) and mid.startswith("claude-"):
            return mid, int(best[0])
    return "", 0


def bridge_started_at(root):
    try:
        with open(os.path.join(root, ".telegram_bot", "runtime-snapshot.json")) as fh:
            stamp = json.load(fh).get("started_at", "")
        return int(datetime.datetime.fromisoformat(stamp).timestamp())
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def agent_label(root):
    try:
        import agent_name
        return agent_name.display_name(root) or os.path.basename(root.rstrip("/"))
    except Exception:
        return os.path.basename(root.rstrip("/"))


def _family(display):
    return " ".join(display.split()[:2])


def check_agents(cfg, alerts, report, instance_roots, wait_secs=0, since=0):
    """Compare each registered agent's bridge model id with the last one seen.

    A version change within the same family (Claude Opus 5 -> Claude Opus
    5.5) is owner notice (c). A family switch is an owner or agent choice
    (/model), not something this job caused: leader queue only. The first
    sighting of an agent only records a baseline.
    """
    path = os.path.join(cfg["state_dir"], "agent-models.json")
    try:
        with open(path) as fh:
            seen = json.load(fh)
    except (OSError, ValueError):
        seen = {}
    rec = {"agents": {}}
    report["agents"] = rec
    if wait_secs > 0 and since > 0:
        deadline = time.time() + wait_secs
        while True:
            pending = [r for r in instance_roots if bridge_started_at(r) >= since
                       and bridge_observed_model(cfg, r)[1] < since]
            if not pending or time.time() >= deadline:
                break
            time.sleep(min(10, max(0.5, deadline - time.time())))
        rec["no_turn_after_restart"] = [agent_label(r) for r in pending]
        for r in pending:
            alerts.add("watch", "agent %s was restarted but reported no turn within %ss "
                       "-- its model is checked again next run" % (agent_label(r), wait_secs))
    for root in instance_roots:
        mid, turn_ts = bridge_observed_model(cfg, root)
        name = agent_label(root)
        rec["agents"][name] = mid
        if not mid:
            continue
        prev = (seen.get(root) or {}).get("id", "")
        if prev and prev != mid:
            old_d = model_display.parse_model_id(prev) or prev
            new_d = model_display.parse_model_id(mid) or mid
            if (model_display.parse_model_id(prev) and model_display.parse_model_id(mid)
                    and _family(old_d) == _family(new_d)):
                report["owner"].agent_changed(name, old_d, new_d)
                alerts.add("ok", "agent %s now runs %s (was %s)" % (name, mid, prev),
                           dedupe=False)
            else:
                alerts.add("watch", "agent %s switched model %s -> %s (family change: an "
                           "owner/agent choice, not pushed)" % (name, prev, mid), dedupe=False)
        seen[root] = {"id": mid, "ts": turn_ts, "name": name}
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(seen, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# owner notices (dec-208): new model / rollback / running-agent model change
# ---------------------------------------------------------------------------

def _fmt(tpl, **kw):
    for k, v in kw.items():
        tpl = tpl.replace("{%s}" % k, str(v))
    return tpl


class OwnerNotices(object):
    """Collects the three owner-grade events of one run and pushes ONE message.

    Copy: config/i18n cli_update.* keys (code carries no owner text). Push:
    the existing proactive push path, only with --owner-push AND
    <state>/owner-push-armed; else the text lands in the ledger as a draft.
    """

    def __init__(self, state_dir, alert_root, push_flag):
        self.state_dir = state_dir
        self.alert_root = alert_root
        self.push_flag = push_flag
        self.models, self.rollbacks, self.agents = [], [], []

    def new_model(self, display):
        if display not in self.models:
            self.models.append(display)

    def rollback(self, product, new, old, verified):
        self.rollbacks.append((product, new, old, verified))

    def agent_changed(self, name, old, new):
        self.agents.append((name, old, new))

    def _seen_path(self):
        return os.path.join(self.state_dir, "owner-notified.json")

    def _lang(self):
        lang = os.environ.get("DOGANY_LANG", "").strip()
        if not lang and self.alert_root:
            lang = read_env_value(os.path.join(self.alert_root, "config", "agent.conf"),
                                  "AGENT_LANG")
        return lang or "en"

    def compose(self, models):
        """Owner text, or raise KeyError(key) when a copy key is missing."""
        lang = self._lang()
        root = os.path.dirname(HERE)
        import i18n_lookup

        def t(key, **kw):
            val = i18n_lookup.lookup(root, lang, "cli_update." + key)
            if not val:
                raise KeyError("cli_update.%s (%s)" % (key, lang))
            return _fmt(val, **kw)
        blocks = []
        for product, new, old, verified in self.rollbacks:
            blocks.append(t("rollback" if verified else "rollback_unverified",
                            product=product, new=new, old=old))
        if len(models) == 1:
            blocks.append(t("new_model_one", model=models[0]))
        elif models:
            blocks.append("\n".join([t("new_model_many")] +
                                    [t("list_row", item=m) for m in models]))
        if len(self.agents) == 1:
            name, old, new = self.agents[0]
            blocks.append(t("agent_model_one", agent=name, old=old, new=new))
        elif self.agents:
            blocks.append("\n".join([t("agent_model_many")] +
                                    [t("agent_model_row", agent=n, old=o, new=w)
                                     for n, o, w in self.agents]))
        return "\n\n".join(blocks)

    def flush(self, alerts, report, now):
        try:
            with open(self._seen_path()) as fh:
                seen = json.load(fh)
        except (OSError, ValueError):
            seen = {}
        models = [m for m in self.models if "model:" + m not in seen]
        if not (models or self.rollbacks or self.agents):
            return
        try:
            text = self.compose(models)
        except (KeyError, ImportError) as e:
            alerts.add("FAIL", "owner notice not sent: copy key missing %s" % e,
                       dedupe=False)
            return
        armed_file = os.path.join(self.state_dir, "owner-push-armed")
        rec = {"text": text, "pushed": False}
        report["owner_notice"] = rec
        if not (self.push_flag and self.alert_root and os.path.exists(armed_file)):
            rec["reason"] = ("no --owner-push" if not self.push_flag else
                             "no alert root" if not self.alert_root else
                             "%s absent" % armed_file)
            drafts = os.path.join(self.state_dir, "owner-notices")
            os.makedirs(drafts, exist_ok=True)
            with open(os.path.join(drafts, time.strftime("%Y%m%d-%H%M%S") + ".txt"),
                      "w") as fh:
                fh.write(text + "\n")
            alerts.add("owner-draft", "owner push not armed (%s); would send: %s"
                       % (rec["reason"], text.replace("\n", " / ")))
            return
        push = os.environ.get("CLI_UPDATER_PUSH", "")
        argv = [push] if push else ["/bin/bash", os.path.join(self.alert_root, "routines",
                                                              "push.sh")]
        argv += ["--text", text, "--audience", "owner",
                 "--env", os.path.join(self.alert_root, ".telegram_bot", ".env")]
        rc, out, err = run_cmd(argv, 120)
        rec["push_rc"] = rc
        if rc != 0:
            alerts.add("FAIL", "owner push failed (rc=%d: %s); text: %s"
                       % (rc, (err or out).strip()[-160:], text.replace("\n", " / ")),
                       dedupe=False)
            return
        rec["pushed"] = True
        for m in models:
            seen["model:" + m] = now
        tmp = self._seen_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(seen, fh, indent=1, sort_keys=True)
        os.replace(tmp, self._seen_path())
        alerts.add("ok", "owner notified: %s" % text.replace("\n", " / "), dedupe=False)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

USAGE = ("usage: cli-updater.py [--mode observe|install|agents] [--alert-root DIR] "
         "[--state-dir DIR] [--instance-root DIR]... [--owner-push] "
         "[--wait-secs N --since EPOCH]\n")


def parse_args(argv):
    args = {"mode": "observe", "alert_root": "", "state_dir": "",
            "instance_roots": [], "owner_push": False, "wait_secs": 0, "since": 0}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--mode", "--alert-root", "--state-dir", "--instance-root",
                 "--wait-secs", "--since"):
            if i + 1 >= len(argv):
                raise ValueError("%s needs a value" % a)
            val = argv[i + 1]
            if a == "--mode":
                if val not in ("observe", "install", "agents"):
                    raise ValueError("bad --mode %s" % val)
                args["mode"] = val
            elif a == "--alert-root":
                args["alert_root"] = val
            elif a == "--state-dir":
                args["state_dir"] = val
            elif a in ("--wait-secs", "--since"):
                if not val.isdigit():
                    raise ValueError("%s needs a number" % a)
                args[a[2:].replace("-", "_")] = int(val)
            else:
                args["instance_roots"].append(val)
            i += 2
        elif a == "--owner-push":
            args["owner_push"] = True
            i += 1
        elif a in ("-h", "--help"):
            sys.stdout.write(USAGE)
            sys.exit(0)
        else:
            raise ValueError("unknown argument %s" % a)
    return args


def main(argv):
    try:
        args = parse_args(argv)
    except ValueError as e:
        sys.stderr.write("[cli-updater] %s\n%s" % (e, USAGE))
        return 2
    cfg = {
        "claude_activator": env("CLI_UPDATER_CLAUDE_ACTIVATOR", home(".local", "bin", "claude")),
        "claude_versions": env("CLI_UPDATER_CLAUDE_VERSIONS",
                               home(".local", "share", "claude", "versions")),
        "claude_json": env("CLI_UPDATER_CLAUDE_JSON", home(".claude.json")),
        "claude_projects": env("CLI_UPDATER_CLAUDE_PROJECTS", home(".claude", "projects")),
        "codex_home": env("CLI_UPDATER_CODEX_HOME", home(".codex")),
        "codex_activator": env("CLI_UPDATER_CODEX_ACTIVATOR", home(".local", "bin", "codex")),
        "tier_tsv": env("CLI_UPDATER_TIER_TSV", os.path.join(LIB, "dispatch-tier-bindings.tsv")),
        "exec_tsv": env("CLI_UPDATER_EXEC_TSV",
                        os.path.join(LIB, "dispatch-runtime-executables.tsv")),
        "display_tsv": env("CLI_UPDATER_DISPLAY_TSV",
                           os.path.join(LIB, "dispatch-model-display.tsv")),
        "state_dir": args["state_dir"] or home(".dogany", "cli-updates"),
        "cooldown": int(env("CLI_UPDATER_COOLDOWN_SECS", "86400")),
        "smoke_timeout": int(env("CLI_UPDATER_SMOKE_TIMEOUT", "180")),
        # Probes stop once this many seconds have passed since start, so the
        # whole run fits the queue's 1200s pre-step cap: the claude install
        # section is bounded by 600 + 60 + 180 = 840s, a codex apply starts
        # only with CODEX_APPLY_RESERVE seconds left, and probes never start
        # past the budget (one in-flight probe <= 180s).
        "budget": int(env("CLI_UPDATER_BUDGET_SECS", "1000")),
    }
    cfg["started"] = time.time()
    os.makedirs(os.path.join(cfg["state_dir"], "runs"), exist_ok=True)
    lock_fh = open(os.path.join(cfg["state_dir"], ".lock"), "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.stderr.write("[cli-updater] another run holds the lock -- exiting\n")
        return 2

    now = now_epoch()
    mode = args["mode"]
    reason = "requested"
    if mode == "install" and not os.path.exists(os.path.join(cfg["state_dir"], "armed")):
        mode, reason = "observe", "not armed: %s/armed absent" % cfg["state_dir"]
    elif mode == "observe":
        reason = "observe requested"
    report = {"ts": now, "mode": mode, "mode_reason": reason,
              "owner": OwnerNotices(cfg["state_dir"], args["alert_root"],
                                    args["owner_push"])}
    alerts = Alerts(cfg["state_dir"], args["alert_root"], bool(args["alert_root"]))
    print("[cli-updater] mode=%s (%s)" % (mode, reason))

    rc = 0
    try:
        if mode == "agents":
            check_agents(cfg, alerts, report, args["instance_roots"],
                         args["wait_secs"], args["since"])
        else:
            # agents first: a change seen now happened BEFORE tonight's install
            check_agents(cfg, alerts, report, args["instance_roots"])
            targets = derive_targets(cfg, alerts, args["instance_roots"])
            report["targets"] = targets
            if targets.get("claude", {}).get("class") == "claude-native":
                rc = update_claude(cfg, mode, alerts, report, now)
            if targets.get("codex", {}).get("class") == "codex-standalone":
                rc = max(rc, update_codex(cfg, mode, alerts, report, now))
            probe_models(cfg, mode, alerts, report)
    except Exception as e:  # fail-open for the queue, loud in the ledger
        alerts.add("FAIL", "cli-updater internal error: %s: %s" % (type(e).__name__, e),
                   dedupe=False)
        rc = 2
    try:
        report["owner"].flush(alerts, report, now)
    except Exception as e:
        alerts.add("FAIL", "owner notice error: %s: %s" % (type(e).__name__, e),
                   dedupe=False)
    report["owner"] = {"models": report["owner"].models,
                       "rollbacks": report["owner"].rollbacks,
                       "agents": report["owner"].agents}
    report["alerts"] = alerts.flush(now)

    c = report.get("claude", {})
    x = report.get("codex", {})
    changed = 1 if c.get("action") == "installed" else 0
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    with open(os.path.join(cfg["state_dir"], "runs",
                           stamp + ("-agents" if mode == "agents" else "") + ".json"),
              "w") as fh:
        json.dump(report, fh, indent=1, sort_keys=True, default=str)
    if mode != "agents":
        # RUN_TS is wall-clock (not the CLI_UPDATER_NOW cooldown clock): the
        # queue's post-step compares it with its own start time to act only
        # on this night's result.
        tmp = os.path.join(cfg["state_dir"], ".last-run.env.tmp")
        with open(tmp, "w") as fh:
            fh.write("RUN_TS=%d\nMODE=%s\nCLAUDE_CHANGED=%d\nCLAUDE_FROM=%s\nCLAUDE_TO=%s\n"
                     "CLAUDE_ACTION=%s\nCODEX_FROM=%s\nCODEX_TO=%s\nCODEX_ACTION=%s\n"
                     % (int(time.time()), mode, changed,
                        c.get("pre_version") or "", c.get("chosen_version") or "",
                        c.get("action") or "none", x.get("installed") or "",
                        x.get("target") or "", x.get("action") or "none"))
        os.replace(tmp, os.path.join(cfg["state_dir"], "last-run.env"))
    print("[cli-updater] done rc=%d claude=%s codex=%s alerts=%d"
          % (rc, c.get("action", "none"), x.get("action", "none"), len(report["alerts"])))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
