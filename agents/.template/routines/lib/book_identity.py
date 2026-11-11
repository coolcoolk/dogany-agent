#!/usr/bin/env python3
"""Identity fingerprints for the BASE resource books (DGN-1979 phase 2, dec-359).

Owner 2026-10-10 17:21 (Q3): books are per account, but they must follow an
account switch, a subscription change and a policy change. Before this, no
book was invalidated on relogin or plan change; they aged out on TTL only
(packs/CONTRACT.md 10-4-1).

A book's identity is a small dict of NON-SECRET components; its fingerprint
is the first 12 hex of sha256 over the canonical JSON of that dict. The
components:

  claude.account   fp of oauthAccount.accountUuid (else emailAddress) in
                   $CLAUDE_CONFIG_DIR/.claude.json or ~/.claude.json
  claude.plan      the plan slug read from the Claude credential
                   (claudeAiOauth.subscriptionType + rateLimitTier), from
                   ~/.claude/.credentials.json, else the keychain item via
                   the secret store (DGN-1981); "unread" when neither says
  codex.login      `codex login status` state via codex_login (DGN-1985)
  codex.method     chatgpt | api_key | -
  codex.account    fp of tokens.account_id in ${CODEX_HOME:-~/.codex}/auth.json
  codex.window     the quota read's windowDurationMins, when the caller has it
  api.key          fp of the pool API key (the DGN-1978 r3 account dir name;
                   the key itself never reaches this module)
  policy.<NAME>    hand-typed inputs: PLAN, API_CREDIT_MONTHLY_USD,
                   API_CREDIT_SEATS, API_CREDIT_SEAT_USD, API_CREDIT_RESERVE_USD

Every account id is fingerprinted before it is stored; no token, key or raw
id is ever written.

Book check (check()): each book keeps the identity it was computed under in
<state dir>/.book-<book>.json. Every read of the book calls check() first.
Same fingerprint -> nothing. Different -> the book's payload files are moved
to <state dir>/book-history/<book>/<utc stamp>-<old fp>/ and made read-only
(0444 history, never read back as current), the new identity is written, and ONE
host decision-ledger line ({"kind": "book-change", ...}, in the host
dispatcher's decisions.jsonl) says what changed. The caller then finds no
current payload and re-reads at once instead of at TTL. No identity on
record yet -> the current one is adopted (no history, no line).

Cheap: the Claude account and credential-file plan are cached by the file's
(mtime, size) in <shared>/books/identity-cache.json; the keychain plan read
is retaken at most every KEYCHAIN_RECHECK_S. Codex login status is a CLI
call, so it is only taken on the Codex book's own read (dispatch), never on
a hook path.

DOGANY_BOOK_PLAN_READ=0 turns the plan read off (fixtures that run against
the real HOME pin PLAN= behavior with it).

Plan read vs. hand-typed (effective_plan()): PLAN= in config/agent.conf is an
OVERRIDE of the plan read when a read exists; with no read it is the plan, as
before. API_CREDIT_MONTHLY_USD has no provider read (no endpoint returns the
grant amount), so it stays the override it is over the plan-derived budget.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CACHE_NAME = "identity-cache.json"
KEYCHAIN_RECHECK_S = 60
CODEX_STATUS_TIMEOUT_S = 5.0
CLAUDE_CRED_SERVICE = "Claude Code-credentials"
POLICY_KEYS = ("PLAN", "API_CREDIT_MONTHLY_USD", "API_CREDIT_SEATS",
               "API_CREDIT_SEAT_USD", "API_CREDIT_RESERVE_USD")
API_ROOT = os.path.join(".dogany", "api-credit")
API_CURRENT = "current.json"
API_SUPERSEDED = "superseded.json"
KNOWN_PLANS = ("pro", "max_5x", "ultra", "max_20x", "team_standard", "team_premium",
               "enterprise", "free")


# --- paths / io -------------------------------------------------------------
def _home(home=None):
    return home or os.path.expanduser("~")


def shared_home(home=None):
    """Same rule as host_dispatcher.shared_home(); `home` (tests, a caller's
    fixture HOME) wins over the process HOME but not over the env pin."""
    return os.environ.get("DOGANY_SHARED_HOME") or os.path.join(_home(home), ".dogany")


def ledger_path(home=None):
    return os.path.join(shared_home(home), "dispatcher", "decisions.jsonl")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, sort_keys=True)
    os.replace(tmp, path)


def _append(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, sort_keys=True) + "\n")


@contextlib.contextmanager
def _lock(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def fp(value):
    """First 12 hex of sha256(value) -- same as api_credit_pool.key_fingerprint."""
    return hashlib.sha256(str(value).strip().encode("utf-8")).hexdigest()[:12]


def fingerprint(components):
    return fp(json.dumps(components, sort_keys=True, separators=(",", ":")))


def _stat_key(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _cache_get(cache, slot, key):
    ent = cache.get(slot)
    if isinstance(ent, dict) and ent.get("key") == key:
        return True, ent.get("value")
    return False, None


# --- component reads ----------------------------------------------------------
def plan_from_oauth(oauth):
    """Plan slug from a claudeAiOauth block, or None when it does not say."""
    if not isinstance(oauth, dict):
        return None
    sub = str(oauth.get("subscriptionType") or "").strip().lower()
    tier = str(oauth.get("rateLimitTier") or "").strip().lower()
    if "max_20x" in tier or "ultra" in tier or sub == "ultra":
        return "ultra"
    if "max_5x" in tier:
        return "max_5x"
    if sub == "team":
        return "team_premium" if "premium" in tier else "team_standard"
    if sub in ("pro", "enterprise", "free"):
        return sub
    return None


def _claude_json_paths(env, home):
    cfg = env.get("CLAUDE_CONFIG_DIR", "")
    return ([os.path.join(cfg, ".claude.json")] if cfg else []) + [os.path.join(_home(home), ".claude.json")]


def _claude_cred_paths(env, home):
    cfg = env.get("CLAUDE_CONFIG_DIR", "")
    return ([os.path.join(cfg, ".credentials.json")] if cfg else []) + \
        [os.path.join(_home(home), ".claude", ".credentials.json")]


def claude_account(env=None, home=None, cache=None):
    """fp of the signed-in Claude account, or "none"."""
    env = os.environ if env is None else env
    cache = {} if cache is None else cache
    for path in _claude_json_paths(env, home):
        key = _stat_key(path)
        if key is None:
            continue
        hit, val = _cache_get(cache, "claude.json:" + path, key)
        if not hit:
            acct = (_read_json(path) or {}).get("oauthAccount") or {}
            v = (acct.get("accountUuid") or acct.get("emailAddress")) if isinstance(acct, dict) else None
            val = fp(v) if v else None
            cache["claude.json:" + path] = {"key": key, "value": val}
        if val:
            return val
    return "none"


def _keychain_backend_allowed():
    if os.environ.get("DOGANY_SECRET_BACKEND", "").strip().lower() == "file":
        return False
    return sys.platform == "darwin"


def claude_plan_read(env=None, home=None, cache=None, now=None):
    """(slug | None, source) read from the Claude credential; never the token."""
    env = os.environ if env is None else env
    cache = {} if cache is None else cache
    now = time.time() if now is None else now
    if os.environ.get("DOGANY_BOOK_PLAN_READ", "1") == "0":
        return None, "plan read off (DOGANY_BOOK_PLAN_READ=0)"
    for path in _claude_cred_paths(env, home):
        key = _stat_key(path)
        if key is None:
            continue
        hit, val = _cache_get(cache, "cred:" + path, key)
        if not hit:
            val = plan_from_oauth((_read_json(path) or {}).get("claudeAiOauth"))
            cache["cred:" + path] = {"key": key, "value": val}
        if val:
            return ("ultra" if val == "max_20x" else val), "credentials file"
    # The keychain item is the host's, not this HOME's: read it only while a
    # Claude account is signed in here (a fixture HOME never reaches it).
    if not _keychain_backend_allowed() or claude_account(env, home, cache) == "none":
        return None, "no plan read"
    ent = cache.get("keychain")
    if isinstance(ent, dict) and now - float(ent.get("at", 0)) < KEYCHAIN_RECHECK_S:
        val = ent.get("value")
    else:
        val = None
        try:
            import secret_store
            raw = secret_store.get(CLAUDE_CRED_SERVICE, "", backend="keychain")
            if raw:
                val = plan_from_oauth((json.loads(raw) or {}).get("claudeAiOauth"))
            raw = None
        except Exception:  # noqa: BLE001 -- an unreadable keychain is "no read"
            val = None
        cache["keychain"] = {"at": now, "value": val}
    return (("ultra" if val == "max_20x" else val), "keychain") if val else (None, "no plan read")


def codex_components(env=None, home=None, window_mins=None, login=True):
    env = os.environ if env is None else env
    st = {"state": "unchecked", "method": None}
    if login:
        try:
            import codex_login
            st = codex_login.status(env, timeout=CODEX_STATUS_TIMEOUT_S) or st
        except Exception:  # noqa: BLE001
            st = {"state": "error", "method": None}
    auth = os.path.join(env.get("CODEX_HOME") or os.path.join(_home(home), ".codex"), "auth.json")
    tok = (_read_json(auth) or {}).get("tokens") or {}
    aid = tok.get("account_id") if isinstance(tok, dict) else None
    comps = {"codex.login": st.get("state") or "error", "codex.method": st.get("method") or "-",
             "codex.account": fp(aid) if aid else "none"}
    if window_mins not in (None, ""):
        comps["codex.window"] = str(window_mins)
    return comps


def codex_account(env=None, home=None):
    """fp of the Codex account in auth.json, or "none" (file read only)."""
    return codex_components(env, home, login=False)["codex.account"]


def conf_values(conf_path, env=None, keys=POLICY_KEYS):
    """env > config/agent.conf, the same precedence dispatch's api_pool_conf
    uses. Empty = unset."""
    env = os.environ if env is None else env
    out = {}
    try:
        from conf_reader import conf_get
    except Exception:  # noqa: BLE001
        conf_get = None
    for k in keys:
        v = (env.get(k) or "").strip()
        if not v and conf_get and conf_path:
            try:
                v = (conf_get(k, conf_path) or "").strip()
            except Exception:  # noqa: BLE001
                v = ""
        out[k] = v
    return out


def effective_plan(typed, read):
    """(slug | "", source text). PLAN= overrides a read; with no read it is
    the plan as before; neither -> ""."""
    typed = (typed or "").strip()
    typed = "ultra" if typed == "max_20x" else typed
    read = "ultra" if read == "max_20x" else read
    if read and typed:
        if typed == read:
            return typed, "plan read %s (PLAN= agrees)" % read
        return typed, "PLAN=%s override (plan read %s)" % (typed, read)
    if read:
        return read, "plan read %s" % read
    if typed:
        return typed, "PLAN=%s (no plan read)" % typed
    return "", "no plan read, PLAN= unset"


# --- cache wrapper ------------------------------------------------------------
@contextlib.contextmanager
def identity_cache(home=None):
    path = os.path.join(shared_home(home), "books", CACHE_NAME)
    cache = _read_json(path) or {}
    before = json.dumps(cache, sort_keys=True)
    yield cache
    if json.dumps(cache, sort_keys=True) != before:
        try:
            _write_json(path, cache)
        except OSError:
            pass


def claude_components(env=None, home=None, plan_typed=None):
    with identity_cache(home) as cache:
        comps = {"claude.account": claude_account(env, home, cache),
                 "claude.plan": claude_plan_read(env, home, cache)[0] or "unread"}
    if plan_typed is not None:
        comps["policy.PLAN"] = plan_typed
    return comps


# --- the book check -------------------------------------------------------------
def _diff(old, new):
    out = []
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            out.append({"what": k, "from": old.get(k, "-"), "to": new.get(k, "-")})
    return out


def _freeze(path):
    """Files read-only; the dir stays removable by the cleanup routine."""
    for name in os.listdir(path):
        try:
            os.chmod(os.path.join(path, name), 0o444)
        except OSError:
            pass


def _ident_path(state_dir, book):
    return os.path.join(state_dir, ".book-%s.json" % book.replace("/", "_"))


def check(book, state_dir, comps, payload=(), home=None, now=None, note=""):
    """-> (verdict new|same|changed, diff list, history dir|""). See header."""
    now = time.time() if now is None else now
    ident = _ident_path(state_dir, book)
    cur = fingerprint(comps)
    os.makedirs(state_dir, exist_ok=True)
    with _lock(ident + ".lock"):
        old = _read_json(ident)
        if not old or not old.get("fp"):
            _write_json(ident, {"book": book, "fp": cur, "components": comps, "at": now})
            return "new", [], ""
        if old["fp"] == cur:
            return "same", [], ""
        changed = _diff(old.get("components") or {}, comps)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        hist = os.path.join(state_dir, "book-history", book.replace("/", "_"), "%s-%s" % (stamp, old["fp"]))
        n = 1
        while os.path.exists(hist):
            n += 1
            hist = os.path.join(os.path.dirname(hist), "%s-%s.%d" % (stamp, old["fp"], n))
        os.makedirs(hist)
        moved = []
        for name in payload:
            src = name if os.path.isabs(name) else os.path.join(state_dir, name)
            if os.path.lexists(src):
                os.replace(src, os.path.join(hist, os.path.basename(src)))
                moved.append(os.path.basename(src))
        _write_json(os.path.join(hist, "book-identity.json"), dict(old, superseded_at=now, superseded_by=cur))
        _freeze(hist)
        _write_json(ident, {"book": book, "fp": cur, "components": comps, "at": now})
        line = {"kind": "book-change", "ts": round(now, 3), "book": book, "from": old["fp"],
                "to": cur, "changed": changed, "invalidated": moved, "history": hist,
                "why": "identity changed: re-read now, not at TTL (DGN-1979 p2)"}
        if note:
            line["note"] = note
        try:
            _append(ledger_path(home), line)
        except OSError:
            pass
    return "changed", changed, hist


def _summary(changed):
    return ", ".join("%s %s->%s" % (c["what"], c["from"], c["to"]) for c in changed) or "-"


# --- book-specific checks ---------------------------------------------------------
def check_api_key(key_fp, home=None, now=None):
    """The pool key's account dir is the key fp (DGN-1978 r3). A rotated key
    -> the previous dir gets a superseded note (kept as history; never the
    current book again unless that key comes back) and one ledger line. The
    new dir starts with no exhausted marker and no reconcile state, so the
    truth is re-read at once (the API's answer, the console report)."""
    now = time.time() if now is None else now
    base = os.path.join(_home(home), API_ROOT)
    verdict, changed, _ = check("api-key", base, {"api.key": key_fp}, home=home, now=now)
    if verdict == "changed":
        old = changed[0]["from"]
        if old and old != "-" and os.path.isdir(os.path.join(base, old)):
            _write_json(os.path.join(base, old, API_SUPERSEDED),
                        {"superseded_by": key_fp, "at": now, "why": "pool API key rotated"})
        try:
            os.unlink(os.path.join(base, key_fp, API_SUPERSEDED))
        except OSError:
            pass
    _write_json(os.path.join(base, API_CURRENT), {"fp": key_fp, "at": now})
    return verdict, changed


def current_api_fp(home=None):
    doc = _read_json(os.path.join(_home(home), API_ROOT, API_CURRENT)) or {}
    return doc.get("fp") or None


def api_book_components(conf_path, env=None, home=None):
    vals = conf_values(conf_path, env)
    with identity_cache(home) as cache:
        read = claude_plan_read(env, home, cache)[0]
    comps = {"claude.plan": read or "unread"}
    comps.update({"policy." + k: v for k, v in vals.items()})
    return comps


def check_api_book(account_dir, conf_path, env=None, home=None, now=None):
    """Plan / policy change on the credit book: the exhausted marker and the
    reconcile state move to history (the ledger rows are spend facts and
    stay); the next decision re-reads both."""
    comps = api_book_components(conf_path, env, home)
    return check("api-credit", account_dir, comps, payload=("exhausted.json", "reconcile.json"),
                 home=home, now=now)


# --- CLI ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="book_identity.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check-claude", help="Claude usage book (host snapshot / usage-gate copy)")
    p.add_argument("--book", required=True)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--payload", action="append", default=[])
    p.add_argument("--conf", default="", help="include PLAN= from this conf")
    p = sub.add_parser("check-codex", help="Codex quota book")
    p.add_argument("--state-dir", default="")
    p.add_argument("--window-mins", default="")
    p = sub.add_parser("check-api-key", help="pool key fp (stdin: the fp, never the key)")
    p = sub.add_parser("check-api-book")
    p.add_argument("--account-dir", required=True)
    p.add_argument("--conf", default="")
    sub.add_parser("plan-read", help="the plan slug read from the Claude credential, or empty")
    p = sub.add_parser("plan", help="effective plan: slug<TAB>source")
    p.add_argument("--conf", default="")
    p = sub.add_parser("show", help="current components of every identity")
    p.add_argument("--conf", default="")
    p.add_argument("--codex", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "check-claude":
        typed = conf_values(a.conf, keys=("PLAN",))["PLAN"] if a.conf else None
        v, changed, _ = check(a.book, a.state_dir, claude_components(plan_typed=typed), payload=a.payload)
        print("%s\t%s" % (v, _summary(changed)))
        return 10 if v == "changed" else 0
    if a.cmd == "check-codex":
        d = a.state_dir or os.path.join(shared_home(), "books", "codex-quota")
        v, changed, _ = check("codex-quota", d, codex_components(window_mins=a.window_mins or None))
        print("%s\t%s" % (v, _summary(changed)))
        return 10 if v == "changed" else 0
    if a.cmd == "check-api-key":
        key_fp = sys.stdin.read().strip()
        if not key_fp or len(key_fp) != 12 or any(c not in "0123456789abcdef" for c in key_fp):
            print("refused\tnot a 12-hex fingerprint")
            return 2
        v, changed = check_api_key(key_fp)
        print("%s\t%s" % (v, _summary(changed)))
        return 10 if v == "changed" else 0
    if a.cmd == "check-api-book":
        v, changed, _ = check_api_book(a.account_dir, a.conf)
        print("%s\t%s" % (v, _summary(changed)))
        return 10 if v == "changed" else 0
    if a.cmd == "plan-read":
        with identity_cache() as cache:
            print(claude_plan_read(cache=cache)[0] or "")
        return 0
    if a.cmd == "plan":
        typed = conf_values(a.conf, keys=("PLAN",))["PLAN"] if a.conf else (os.environ.get("PLAN") or "")
        with identity_cache() as cache:
            read = claude_plan_read(cache=cache)[0]
        slug, src = effective_plan(typed, read)
        print("%s\t%s" % (slug, src))
        return 0
    if a.cmd == "show":
        typed = conf_values(a.conf, keys=("PLAN",))["PLAN"] if a.conf else None
        out = claude_components(plan_typed=typed)
        if a.codex:
            out.update(codex_components())
        out["api.key"] = current_api_fp() or "none"
        print(json.dumps(out, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
