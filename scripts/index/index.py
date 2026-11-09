#!/usr/bin/env python3
"""index.py -- shared index resolver core (DGN-1883/DGN-1893 design v2 sec 2, WP1).

One mechanism, two index types (catalog, knowledge): a git repo fetched into a
machine cache, extracted into a verified snapshot, and published by an atomic
`current` symlink swap. stdlib only.

Cache (per type + channel, shared by every instance on the machine):
  <home>/index/<type>/<channel>/
    repo/          clone --depth 1 --single-branch --no-checkout ONCE; every
                   later refresh is a plain `git fetch origin <ref>` (no depth
                   flags -- the is-ancestor check needs the old tip reachable)
    snap/<commit>/ git archive of a verified commit
    current        symlink -> snap/<commit>, swapped with os.replace()
    state.json     url, url_origin, ref, commit, fetched_at, verified_at,
                   last_attempt, last_error, schema_version,
                   quarantined_commit, rejected_commit
  <home>/index/<type>.<channel>.lock   fcntl.flock, 20 s wait
<home> = $DOGANY_SHARED_HOME or ~/.dogany.

Trust (sec 2.3): allowlist on the framework-default URL (env / machine-conf
URLs are the machine owner's own and exempt, recorded url_origin=override);
history rewrite (new tip not a descendant of current) = refuse + quarantine;
schema gate; catalog lint. Nothing from the fetched tree is ever executed.

Network: GIT_TERMINAL_PROMPT=0 + GCM_INTERACTIVE=never only. The ambient git
credential helper is deliberately KEPT (design N1): the catalog repo is
private and the owner machine reads it through it. A machine without access
fails fast (no prompt) and keeps whatever `current` it has.

CLI: index.py {refresh|path|status} <catalog|knowledge>
              [--instance <root> | --channel dev|stable] [--if-older <sec>]
  refresh  rc 0 = current valid (advanced, unchanged, or skipped by
           --if-older / lock loser); rc 1 = this attempt failed or was
           refused (current untouched, last_error set). Either way stdout
           carries the current snapshot dir when one exists.
  path     never touches the network; prints the current snapshot dir
           (rc 0) or nothing (rc 1).
  status   key=value lines from state.json plus age.
  rc 2 = usage (bad type / unknown channel).
"""
import argparse
import errno
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time

RESOLVER_VERSION = "0.1.0"
SUPPORTED_KNOWLEDGE_SCHEMA = 1
TYPES = ("catalog", "knowledge")
GIT_TIMEOUT = 20
LOCK_WAIT = 20
KEEP_SNAPS = 2
TMP_MAX_AGE = 3600

HERE = os.path.dirname(os.path.abspath(__file__))
FRAMEWORK_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
from catalog_lint import lint_catalog  # noqa: E402


class ResolveError(Exception):
    """A refresh failure with a short kind tag (recorded in state.json)."""

    def __init__(self, kind, msg):
        Exception.__init__(self, msg)
        self.kind = kind


# --------------------------------------------------------------- config ----

def shared_home():
    return os.environ.get("DOGANY_SHARED_HOME") or os.path.join(
        os.path.expanduser("~"), ".dogany")


def load_conf(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def default_conf_path():
    return os.environ.get("DOGANY_INDEX_DEFAULT_CONF") or os.path.join(
        FRAMEWORK_ROOT, "config", "index-sources.default")


def resolve_config(itype, channel):
    """env > ~/.dogany/index-sources.conf > framework default (sec 2.1)."""
    dflt = load_conf(default_conf_path())
    mach = load_conf(os.path.join(shared_home(), "index-sources.conf"))
    env_pfx = "DOGANY_INDEX_%s_" % itype.upper()
    url, origin = os.environ.get(env_pfx + "URL", ""), "override"
    if not url:
        url = mach.get(itype + ".url", "")
    if not url:
        url, origin = dflt.get(itype + ".url", ""), "default"
    ref_key = "%s.ref.%s" % (itype, channel)
    ref = (os.environ.get(env_pfx + "REF", "") or mach.get(ref_key, "")
           or dflt.get(ref_key, ""))

    def num(key, fallback):
        try:
            return int(mach.get(key) or dflt.get(key) or fallback)
        except ValueError:
            return fallback
    return {
        "type": itype, "channel": channel, "url": url, "url_origin": origin,
        "ref": ref, "ttl": num(itype + ".ttl", 1800),
        "backoff": num("backoff", 300),
        "allow_prefix": mach.get("allow.url.prefix") or dflt.get("allow.url.prefix", ""),
    }


def channel_for_instance(root):
    """.instance.conf DOGANY_UPDATE_CHANNEL: dev -> dev; release/staging/main
    or undeclared -> stable (update.sh's undeclared default); anything else
    is an operator error (update.sh refuses an unknown channel too)."""
    val = ""
    try:
        with open(os.path.join(root, ".instance.conf"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DOGANY_UPDATE_CHANNEL="):
                    val = line.split("=", 1)[1].strip().strip("\"'")
                    break
    except OSError:
        pass
    if val == "dev":
        return "dev"
    if val in ("", "release", "staging", "main"):
        return "stable"
    raise ResolveError("usage", "unknown DOGANY_UPDATE_CHANNEL '%s' in %s" % (val, root))


def cache_dir(itype, channel):
    return os.path.join(shared_home(), "index", itype, channel)


# ---------------------------------------------------------------- state ----

STATE_KEYS = ("url", "url_origin", "ref", "commit", "fetched_at", "verified_at",
              "last_attempt", "last_error", "schema_version",
              "quarantined_commit", "rejected_commit")


def read_state(d):
    try:
        with open(os.path.join(d, "state.json"), encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict):
            return st
    except (OSError, ValueError):
        pass
    return {}


def write_state(d, st):
    tmp = os.path.join(d, ".state.json.tmp-%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({k: st.get(k) for k in STATE_KEYS}, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, os.path.join(d, "state.json"))


def current_snapshot(d):
    cur = os.path.join(d, "current")
    if os.path.islink(cur) and os.path.isdir(cur):
        return os.path.realpath(cur)
    return ""


# ------------------------------------------------------------------ git ----

def git_env():
    env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(k, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def git(args, cwd=None, timeout=GIT_TIMEOUT, check=True, stdout=subprocess.PIPE):
    cmd = ["git"] + (["-C", cwd] if cwd else []) + args
    try:
        p = subprocess.run(cmd, env=git_env(), stdout=stdout, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ResolveError("network", "git %s timed out after %ds" % (args[0], timeout))
    except OSError as e:
        raise ResolveError("network", "git not runnable: %s" % e)
    if check and p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise ResolveError("network", "git %s rc=%d: %s"
                          % (args[0], p.returncode, err[-1] if err else ""))
    return p


def has_commit(repo, sha):
    return git(["cat-file", "-e", sha + "^{commit}"], cwd=repo, check=False).returncode == 0


def fetch_tip(d, cfg):
    """Clone once / plain fetch. Returns (new_commit, fresh_url)."""
    repo = os.path.join(d, "repo")
    if os.path.isdir(os.path.join(repo, ".git")) or os.path.isfile(os.path.join(repo, "HEAD")):
        origin = git(["config", "--get", "remote.origin.url"], cwd=repo, check=False)
        if origin.stdout.decode().strip() == cfg["url"]:
            git(["fetch", "-q", "origin", cfg["ref"]], cwd=repo)
            tip = git(["rev-parse", "FETCH_HEAD^{commit}"], cwd=repo)
            return tip.stdout.decode().strip(), False
    tmp = os.path.join(d, ".repo-tmp-%d" % os.getpid())
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        git(["clone", "-q", "--depth", "1", "--single-branch", "-b", cfg["ref"],
             "--no-checkout", cfg["url"], tmp])
        tip = git(["rev-parse", "HEAD^{commit}"], cwd=tmp).stdout.decode().strip()
        shutil.rmtree(repo, ignore_errors=True)
        os.rename(tmp, repo)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # A repo that existed under a different URL was replaced: the old tip
    # belongs to another history, so ancestry is trust-on-first-use again.
    return tip, True


def is_descendant(d, old, new):
    repo = os.path.join(d, "repo")
    if not has_commit(repo, old):
        # repo/ was lost or re-cloned shallow while snapshots survived: deepen
        # once so the ancestry question has an answer instead of a guess.
        git(["fetch", "-q", "--unshallow", "origin", new], cwd=repo, check=False)
        if not has_commit(repo, old):
            return False
    return git(["merge-base", "--is-ancestor", old, new], cwd=repo,
               check=False).returncode == 0


def extract(d, commit, dest):
    repo = os.path.join(d, "repo")
    p = subprocess.Popen(["git", "-C", repo, "archive", "--format=tar", commit],
                         env=git_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL)
    try:
        with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
            if hasattr(tarfile, "data_filter"):
                tf.extractall(dest, filter="data")
            else:
                tf.extractall(dest)
    finally:
        p.stdout.close()
        rc = p.wait(timeout=GIT_TIMEOUT)
    if rc != 0:
        raise ResolveError("extract", "git archive rc=%d" % rc)


# --------------------------------------------------------------- verify ----

def _semver(v):
    try:
        return tuple(int(x) for x in str(v).split("-")[0].split("."))
    except ValueError:
        return None


def verify_snapshot(itype, cfg, snap):
    """Returns schema_version; raises ResolveError (kind lint|schema)."""
    if itype == "catalog":
        try:
            with open(os.path.join(snap, "catalog.json"), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            raise ResolveError("lint", "catalog.json unreadable: %s" % e)
        errs, too_new = lint_catalog(data, cfg["channel"], cfg["allow_prefix"])
        if too_new:
            raise ResolveError("schema", errs[0])
        if errs:
            raise ResolveError("lint", "; ".join(errs))
        return data.get("version")
    try:
        with open(os.path.join(snap, "modules.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise ResolveError("lint", "modules.json unreadable: %s" % e)
    if not isinstance(data, dict):
        raise ResolveError("lint", "modules.json is not an object")
    sv = data.get("schema_version")
    if not isinstance(sv, int) or isinstance(sv, bool):
        raise ResolveError("lint", "modules.json schema_version missing or not an integer")
    if sv > SUPPORTED_KNOWLEDGE_SCHEMA:
        raise ResolveError("schema", "knowledge schema_version %d > supported %d"
                          % (sv, SUPPORTED_KNOWLEDGE_SCHEMA))
    need, have = _semver(data.get("min_resolver", "0")), _semver(RESOLVER_VERSION)
    if need is None:
        raise ResolveError("lint", "modules.json min_resolver unparsable")
    if need > have:
        raise ResolveError("schema", "knowledge min_resolver %s > resolver %s"
                          % (data.get("min_resolver"), RESOLVER_VERSION))
    # Per-module sha256 vs the public manifest (sec 2.3 #6) belongs to the
    # view builder's per-module fallback (WP9), not to the snapshot gate.
    return sv


# ----------------------------------------------------------------- lock ----

def acquire_lock(itype, channel):
    """Returns (fh, waited). fh None = gave up after LOCK_WAIT."""
    base = os.path.join(shared_home(), "index")
    os.makedirs(base, exist_ok=True)
    fh = open(os.path.join(base, "%s.%s.lock" % (itype, channel)), "a")
    deadline, waited = time.time() + LOCK_WAIT, False
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh, waited
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
        waited = True
        if time.time() >= deadline:
            fh.close()
            return None, True
        time.sleep(0.1)


# ------------------------------------------------------------------- gc ----

def gc_snaps(d):
    sd = os.path.join(d, "snap")
    keep = os.path.basename(current_snapshot(d))
    try:
        names = os.listdir(sd)
    except OSError:
        return
    now = time.time()
    snaps = []
    for n in names:
        p = os.path.join(sd, n)
        try:
            mt = os.lstat(p).st_mtime
        except OSError:
            continue
        if n.startswith(".tmp-"):
            if now - mt > TMP_MAX_AGE:
                shutil.rmtree(p, ignore_errors=True)
            continue
        snaps.append((mt, n))
    snaps.sort(reverse=True)
    for _, n in snaps[KEEP_SNAPS:]:
        if n != keep:
            shutil.rmtree(os.path.join(sd, n), ignore_errors=True)


# -------------------------------------------------------------- refresh ----

def alert(cfg, kind, commit, msg):
    sys.stderr.write("[index][ALERT] %s/%s %s at %s: %s\n"
                     % (cfg["type"], cfg["channel"], kind, commit[:12], msg))


def refresh(itype, channel, if_older=None):
    cfg = resolve_config(itype, channel)
    d = cache_dir(itype, channel)
    os.makedirs(d, exist_ok=True)
    if if_older is not None:
        st = read_state(d)
        gate = max((st.get("fetched_at") or 0) + if_older,
                   (st.get("last_attempt") or 0) + cfg["backoff"])
        if time.time() < gate:
            return 0
    fh, waited = acquire_lock(itype, channel)
    if fh is None or waited:
        # Someone else refreshed (or is still refreshing): no double fetch.
        if fh is not None:
            fh.close()
        return 0
    try:
        return _refresh_locked(cfg, d)
    finally:
        fh.close()


def _refresh_locked(cfg, d):
    st = read_state(d)
    st["last_attempt"] = int(time.time())
    st.update(url=cfg["url"], url_origin=cfg["url_origin"], ref=cfg["ref"])
    try:
        write_state(d, st)
        if not cfg["url"] or not cfg["ref"]:
            raise ResolveError("config", "no url/ref configured for %s/%s"
                              % (cfg["type"], cfg["channel"]))
        if cfg["url_origin"] == "default" and not (
                cfg["allow_prefix"] and cfg["url"].startswith(cfg["allow_prefix"])):
            raise ResolveError("allowlist", "default url %s is off the allowlist (%s)"
                              % (cfg["url"], cfg["allow_prefix"]))
        new, fresh_url = fetch_tip(d, cfg)
        st["fetched_at"] = int(time.time())
        cur_snap = current_snapshot(d)
        cur = os.path.basename(cur_snap) if cur_snap else ""
        if cur and new == cur:
            st["last_error"] = None
            st["commit"] = cur
            write_state(d, st)
            return 0
        if cur and not fresh_url and not is_descendant(d, cur, new):
            if st.get("quarantined_commit") != new:
                alert(cfg, "history-rewrite", new,
                      "new tip is not a descendant of current %s; kept current" % cur[:12])
            st["quarantined_commit"] = new
            raise ResolveError("rewrite", "history rewrite refused: %s does not descend from %s"
                              % (new[:12], cur[:12]))
        sd = os.path.join(d, "snap")
        os.makedirs(sd, exist_ok=True)
        final = os.path.join(sd, new)
        tmp = os.path.join(sd, ".tmp-%s-%d" % (new, os.getpid()))
        try:
            if not os.path.isdir(final):
                shutil.rmtree(tmp, ignore_errors=True)
                os.mkdir(tmp)
                extract(d, new, tmp)
                schema = verify_snapshot(cfg["type"], cfg, tmp)
                os.rename(tmp, final)
            else:
                schema = verify_snapshot(cfg["type"], cfg, final)
        except ResolveError as e:
            if e.kind in ("lint", "schema") and st.get("rejected_commit") != new:
                alert(cfg, "update-needed" if e.kind == "schema" else "lint-reject", new, str(e))
            if e.kind in ("lint", "schema"):
                st["rejected_commit"] = new
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        link_tmp = os.path.join(d, ".current.tmp")
        if os.path.lexists(link_tmp):
            os.unlink(link_tmp)
        os.symlink(os.path.join("snap", new), link_tmp)
        os.replace(link_tmp, os.path.join(d, "current"))
        st.update(commit=new, verified_at=int(time.time()), last_error=None,
                  schema_version=schema)
        write_state(d, st)
        gc_snaps(d)
        return 0
    except ResolveError as e:
        st["last_error"] = "%s: %s" % (e.kind, e)
    except OSError as e:
        st["last_error"] = "io: %s" % e
    try:
        write_state(d, st)
    except OSError:
        pass
    sys.stderr.write("[index] %s/%s refresh failed: %s\n"
                     % (cfg["type"], cfg["channel"], st["last_error"]))
    return 1


# ------------------------------------------------------------------ cli ----

def main(argv=None):
    ap = argparse.ArgumentParser(prog="index.py")
    ap.add_argument("verb", choices=("refresh", "path", "status"))
    ap.add_argument("type")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--instance")
    grp.add_argument("--channel", choices=("dev", "stable"))
    ap.add_argument("--if-older", type=int)
    a = ap.parse_args(argv)
    if a.type not in TYPES:
        sys.stderr.write("index: unknown type '%s' (valid: %s)\n" % (a.type, "|".join(TYPES)))
        return 2
    try:
        channel = a.channel or (channel_for_instance(a.instance) if a.instance else "stable")
    except ResolveError as e:
        sys.stderr.write("index: %s\n" % e)
        return 2
    d = cache_dir(a.type, channel)
    if a.verb == "status":
        st = read_state(d)
        now = int(time.time())
        print("type=%s" % a.type)
        print("channel=%s" % channel)
        for k in STATE_KEYS:
            print("%s=%s" % (k, "" if st.get(k) is None else st.get(k)))
        fa = st.get("fetched_at")
        print("age=%s" % ("" if not fa else now - fa))
        print("current=%s" % current_snapshot(d))
        return 0
    rc = 0
    if a.verb == "refresh":
        rc = refresh(a.type, channel, a.if_older)
    cur = current_snapshot(d)
    if cur:
        print(cur)
    elif a.verb == "path":
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
