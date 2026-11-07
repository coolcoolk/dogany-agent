#!/usr/bin/env python3
# semver_range.py -- SINGLE SOURCE of the dogany pack semver range grammar +
# satisfaction algorithm (DGN-1031 residual debt 1: the algorithm previously
# existed as two lockstep copies -- compat-lint.sh's _semver_satisfies /
# _is_semver_range heredocs and update.sh's fw_reqframework_guard() inline
# python -- and update_plan.sh made the consumer count three via sed
# extraction of the guard. One more slice and a fourth copy was coming).
#
# Grammar (deliberately narrow -- exactly the subset dogany pack manifests
# use; DO NOT grow it here without a spec: ^ and ~ stay unsupported):
#   range = one or more whitespace-separated constraint tokens, AND-combined
#   token = (>=|>|<=|<|==|!=) X.Y.Z
#   Version parsing is a PREFIX match on X.Y.Z -- trailing characters are
#   ignored ('1.2.3-rc1' compares as (1,2,3)), preserving the shipped
#   behavior of both prior copies byte-for-byte -- for PLAIN bounds only.
#   DGN-1853: a bound carrying a pre-release (>=2.6.0-dev.72) is compared by
#   SemVer 2.0 §11 precedence (cmp_precedence); the version must then
#   strict-parse or the token is unevaluable (None / CLI exit 2).
#
# Consumers (all thin wrappers -- no copies of the algorithm remain):
#   - scripts/pack/compat-lint.sh   _semver_satisfies / _is_semver_range
#     call the CLI below (exit-code contract identical to the old heredocs,
#     stderr messages included).
#   - update.sh fw_reqframework_guard() loads this file via importlib
#     (path handed in as argv; missing/broken module = guard FAIL, exit 1 --
#     fail closed, never "0 constraints checked, pass").
#   - scripts/pack/update_plan.sh inherits the guard by sed extraction and
#     points it here via DOGANY_SEMVER_LIB.
#
# CLI:
#   semver_range.py is-range  <string>            exit 0 valid / 1 invalid
#   semver_range.py satisfies <version> <range>   exit 0 satisfied
#                                                 exit 1 NOT satisfied
#                                                 exit 2 parse/grammar error
#                                                        (stderr says why)
#   semver_range.py compare   <a> <b>             prints -1 / 0 / 1 (a vs b,
#                                                 SemVer 2.0 §11 precedence,
#                                                 pre-release aware), exit 0;
#                                                 exit 2 on an unparseable
#                                                 version (stderr says which)
#   semver_range.py compare-pack <id> <a> <b>   compare with <id>'s legacy
#                                                 stamps mapped (DGN-2119)
#   semver_range.py display <id> <version>      owner-facing version text
#   semver_range.py pick-tag <id> <channel>     newest channel tag (stdin)
#   semver_range.py sort-tags <id>              stdin tags, newest first
#   semver_range.py dev-tail <id>               exit 0 iff <id> is in the
#                                                 dev-tail tables above
#   Anything else: usage on stderr, exit 2 (error lane -- a misuse must
#   never read as "satisfied" or "valid range").
#
# PRECEDENCE LANE (dsp t2pin -- pre-release ledger honesty). The range lane
# above deliberately PREFIX-parses versions ('1.2.3-rc1' compares as (1,2,3),
# byte-for-byte with both prior copies) -- that is correct for
# requires_framework/requires_kit range checks, where a dev build of X.Y.Z
# must satisfy the same ranges as X.Y.Z. It is WRONG for ordering an
# INSTALLED version against a CANDIDATE: with a pre-release ledger entry
# (pack_health@0.11.0-dev.1) the prefix parse reads dev.1 == 0.11.0, so the
# real 0.11.0 cut would be judged "already installed" and the upgrade never
# detected. compare()/cmp_precedence() below is that missing ordering:
# full SemVer 2.0 §11 (release > its pre-releases; pre-release identifiers
# compared dot-by-dot, numeric < alphanumeric; build metadata ignored).
# NOTHING in the range lane changed -- parse_semver/is_semver_range/
# satisfies are untouched and every existing consumer stays byte-identical.
# (DGN-1853 later routed pre-release-carrying BOUNDS through
# cmp_precedence -- see _eval_token; plain bounds still prefix-parse.)
#
# Library:
#   parse_semver(s)        -> (major, minor, patch) or None
#   parse_semver_full(s)   -> ((major, minor, patch), prerelease-tuple|None)
#                             or None; strict X.Y.Z[-pre][+build] shape
#   cmp_precedence(a, b)   -> -1 / 0 / 1 or None (either side unparseable)
#   is_semver_range(s)     -> bool
#   satisfies(ver, range)  -> True / False / None
#     None = unevaluable (unparseable version, unparseable constraint,
#     unsupported operator, malformed token). Every consumer maps None to a
#     fail-closed verdict. Divergence note (measured before unification):
#     the old update.sh copy CRASHED (AttributeError) on a token outside the
#     operator set where the old compat-lint copy exited 2; both were
#     blocking lanes and both sat behind is_semver_range() gating. Unified
#     to the DEFINED error (None here, exit 2 on the CLI) -- same fail-closed
#     direction, no reachable verdict change.

import re
import sys

# Strict token grammar -- the single truth is_semver_range() checks against.
# No end anchor after the numeric triple: '>=1.2.3-rc' is a valid token
# (grammar kept); since DGN-1853 its pre-release is honoured at evaluation.
_VALID_OP = re.compile(r'^(>=|>|<=|<|==|!=)\d+\.\d+\.\d+')

# Token splitter for satisfies(): recognizes ^/~ as EXPLICITLY unsupported
# (distinct diagnostic) exactly like the old compat-lint heredoc did.
_TOKEN_RE = re.compile(r'^(>=|>|<=|<|==|!=|\^|~)(.+)$')


def parse_semver(s):
    """Prefix-parse X.Y.Z -> (int, int, int); None if no numeric triple."""
    m = re.match(r'^(\d+)\.(\d+)\.(\d+)', s.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


# Strict full-version shape for the PRECEDENCE lane only (the range lane
# keeps its prefix parse). Rejects what it cannot order instead of guessing:
# an unparseable version must fail closed at the caller, never sort somewhere.
# DGN-1509: do NOT relax this to a prefix parse (matching parse_semver()'s
# range-lane convention above) without re-reading DGN-1509 SPEC v6 §1.2 and
# the v6-recheck §3/§8-2 first. This end-anchored, full-string match is the
# ONLY gate in the update_plan.sh/update_apply.sh candidate path that
# rejects a '/' or '_' riding through in a tag's version segment (the tag
# enumeration glob :243 and the prefix pin gate :755-757 are both bash
# pattern matches that let either through). A prefix relaxation here would
# silently let 'pack/a/v1.0.0_v2' and 'pack/a/v1.0.0/v2' both become valid
# candidate versions -- the crew-transaction persistent-defer alarm
# normalizes '/' to '_' for its dedup key (agents/.template/routines/
# cron-guard.sh _crow_alarm_key), so those two DIFFERENT transactions would
# collide on the SAME key and one alarm would permanently swallow the
# other's (measured, tests/dgn1509_lease_alarm_selftest.sh K2/K2b). The
# collision pair is pinned as a gate-rejected fixture in
# scripts/tests/test-pack-prerelease-ledger.sh so relaxing this goes red
# there before it ships.
_FULL_RE = re.compile(
    r'^(\d+)\.(\d+)\.(\d+)'
    r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
    r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$')


def parse_semver_full(s):
    """Strict-parse X.Y.Z[-pre][+build] -> ((X,Y,Z), pre-tuple|None); None
    if the string is not a full semver version. Build metadata is consumed
    and discarded (SemVer §10: no precedence weight)."""
    m = _FULL_RE.match(s.strip())
    if not m:
        return None
    base = tuple(int(x) for x in m.groups()[:3])
    pre = m.group(4)
    return (base, tuple(pre.split('.')) if pre is not None else None)


def cmp_precedence(a_str, b_str):
    """SemVer 2.0 §11 precedence: -1 (a<b) / 0 / 1 (a>b); None if either
    side does not strict-parse."""
    a = parse_semver_full(a_str)
    b = parse_semver_full(b_str)
    if a is None or b is None:
        return None
    if a[0] != b[0]:
        return -1 if a[0] < b[0] else 1
    ap, bp = a[1], b[1]
    if ap is None and bp is None:
        return 0
    if ap is None:
        return 1   # release > any of its pre-releases
    if bp is None:
        return -1
    for x, y in zip(ap, bp):
        if x == y:
            continue
        xd, yd = x.isdigit(), y.isdigit()
        if xd and yd:
            return -1 if int(x) < int(y) else 1
        if xd:
            return -1  # numeric identifiers < alphanumeric identifiers
        if yd:
            return 1
        return -1 if x < y else 1
    if len(ap) == len(bp):
        return 0
    return -1 if len(ap) < len(bp) else 1  # shorter prefix-equal set < longer


# DEV-TAIL LANE (DGN-2119 / dec-446). An UNRELEASED product keeps its
# release number fixed (0.1 -> 0.1.0) until an owner-approved release; each
# internal delivery bumps only the dev.N tail (0.1.0-dev.1, -dev.2, ...;
# framework precedent v2.7.0-dev.3). Before dec-446 the two dev-crew products
# were stamped with patch bumps just to make an update visible (dec-440); those
# LEGACY stamps (pack tags + .instance.conf DOGANY_PACKS records) are mapped
# here onto the dev tail so an installed 0.1.1 is never read as newer than
# its next dev tail. The table is CLOSED: it only names stamps that already
# exist (tags pack/kit_dev/v0.1.1, v0.1.2, pack/agent_dev/v0.1.0, v0.1.1) and
# never grows -- new deliveries carry a real dev tail. Map per the DGN-2119
# spec: kit_dev 0.1.1/0.1.2 -> dev.1/dev.2 (dec-461 fixed the earlier
# one-lower map), agent_dev 0.1.0/0.1.1 -> dev.0/dev.1. So the next deliveries
# are kit_dev 0.1.0-dev.3 and agent_dev 0.1.0-dev.2. A legacy stamp orders
# just below the dev-tail version it maps to.
LEGACY_DEV_STAMPS = {
    'kit_dev': {'0.1.1': '0.1.0-dev.1', '0.1.2': '0.1.0-dev.2'},
    'agent_dev': {'0.1.0': '0.1.0-dev.0', '0.1.1': '0.1.0-dev.1'},
}
# Products with NO owner-approved release yet. While listed: the stable
# channel falls back to the newest dev-tail tag (there is no stable tag to
# offer, and the dev tail IS the delivery lane). Owner-facing text is raw
# semver for every product (dec-461): "v0.1.0-dev.2", same as the framework's
# "v2.7.0-dev.4" -- a legacy stamp shows the dev tail it maps to. The
# owner-gated release removes the id here (one line).
UNRELEASED_PRODUCTS = frozenset({'kit_dev', 'agent_dev'})


def pack_version(pack_id, version):
    """The precedence-bearing version of a pack stamp: a legacy stamp maps
    onto its dev tail; everything else is returned unchanged (stripped)."""
    v = (version or '').strip()
    return LEGACY_DEV_STAMPS.get(pack_id or '', {}).get(v, v)


def cmp_pack(pack_id, a_str, b_str):
    """cmp_precedence over pack_version(); identical to cmp_precedence for a
    pack with no legacy stamps. None when either side does not strict-parse
    (checked on the RAW string first: the map never rescues garbage). A
    legacy stamp and the dev tail it maps to are not equal: the legacy one
    orders just below, so the real dev-tail build replaces it."""
    if parse_semver_full(a_str) is None or parse_semver_full(b_str) is None:
        return None
    na, nb = pack_version(pack_id, a_str), pack_version(pack_id, b_str)
    c = cmp_precedence(na, nb)
    if c == 0 and a_str.strip() != b_str.strip():
        legacy = LEGACY_DEV_STAMPS.get(pack_id or '', {})
        al, bl = a_str.strip() in legacy, b_str.strip() in legacy
        if al != bl:
            return -1 if al else 1
    return c


def is_prerelease_pack(pack_id, version):
    """True iff the stamp is a pre-release once legacy stamps are mapped.
    None when it does not strict-parse."""
    full = parse_semver_full(pack_version(pack_id, version))
    if full is None:
        return None
    return full[1] is not None


def display_version(pack_id, version):
    """Owner-facing version text (dec-461: raw semver, one format for every
    product): "v<semver>" with a legacy stamp mapped onto its dev tail
    (kit_dev 0.1.2 -> "v0.1.0-dev.2"). Only a dev-tail product (legacy map
    or UNRELEASED) is rendered here; anything else, or an unparseable stamp,
    returns None so the caller keeps its own (identical) "v<version>"."""
    pid = pack_id or ''
    if pid not in UNRELEASED_PRODUCTS and pid not in LEGACY_DEV_STAMPS:
        return None
    mapped = pack_version(pid, version)
    if parse_semver_full(mapped) is None:
        return None
    return 'v' + mapped


def sort_tags(pack_id, tags):
    """pack/<id>/v* tags newest first by cmp_pack (legacy-aware); tags that do
    not strict-parse keep their input order after the parseable ones."""
    import functools
    prefix = 'pack/%s/v' % pack_id
    ok, rest = [], []
    for t in tags:
        t = t.strip()
        if not t:
            continue
        v = t[len(prefix):] if t.startswith(prefix) else ''
        (ok if v and parse_semver_full(v) is not None else rest).append((t, v))
    ok.sort(key=functools.cmp_to_key(lambda a, b: cmp_pack(pack_id, b[1], a[1])))
    return [t for t, _ in ok] + [t for t, _ in rest]


def pick_tag(pack_id, channel, tags):
    """Newest pack/<id>/v<ver> tag for a channel, legacy-aware: dev = any;
    stable = no pre-release (legacy stamps count as pre-release), and for an
    UNRELEASED product with no stable tag, the newest dev tail. Tags whose
    version does not strict-parse are skipped. '' when nothing qualifies."""
    prefix = 'pack/%s/v' % pack_id
    pool = []
    for t in tags:
        t = t.strip()
        if not t.startswith(prefix):
            continue
        v = t[len(prefix):]
        pre = is_prerelease_pack(pack_id, v)
        if pre is None:
            continue
        pool.append((t, v, pre))
    if channel != 'dev':
        stable = [e for e in pool if not e[2]]
        if stable or pack_id not in UNRELEASED_PRODUCTS:
            pool = stable
    best = None
    for e in pool:
        if best is None or cmp_pack(pack_id, e[1], best[1]) > 0:
            best = e
    return best[0] if best else ''


def is_semver_range(s):
    """True iff s is 1+ whitespace-separated strict constraint tokens."""
    tokens = s.split()
    if not tokens:
        return False
    return all(_VALID_OP.match(t) for t in tokens)


def _eval_token(ver, token, ver_str=None):
    """Evaluate one constraint token against a parsed version tuple.

    Returns a pair (kind, value):
      ('ok', bool)          -- constraint evaluated; bool = holds?
      ('badtoken', token)   -- token matches no known operator shape
      ('badcv', cv_str)     -- operator ok, constraint version unparseable
      ('badop', op)         -- ^ / ~: recognized but unsupported
      ('badver', ver_str)   -- pre-release bound, but the version does not
                               strict-parse, so it cannot be ordered
    Check order (badtoken -> badcv -> badop) mirrors the old compat-lint
    heredoc so CLI diagnostics stay byte-identical.

    DGN-1853: a bound that CARRIES a pre-release ('>=2.6.0-dev.72') is
    compared by SemVer 2.0 §11 precedence (cmp_precedence) -- the prefix
    parse read it as '>=2.6.0' and let dev.70/71 through. A PLAIN bound
    ('>=2.6.0') keeps the prefix parse: a dev build of X.Y.Z satisfies the
    same plain ranges as X.Y.Z (2.6.0-dev.N satisfies >=2.6.0), unchanged.
    """
    m = _TOKEN_RE.match(token)
    if not m:
        return ('badtoken', token)
    op, cv_str = m.group(1), m.group(2)
    cv = parse_semver(cv_str)
    if cv is None:
        return ('badcv', cv_str)
    if op in ('^', '~'):
        return ('badop', op)
    cv_full = parse_semver_full(cv_str)
    if cv_full is not None and cv_full[1] is not None:
        c = cmp_precedence(ver_str, cv_str) if ver_str is not None else None
        if c is None:
            return ('badver', ver_str)
        result = {'>=': c >= 0, '>': c > 0, '<=': c <= 0,
                  '<': c < 0, '==': c == 0, '!=': c != 0}[op]
        return ('ok', result)
    result = {'>=': ver >= cv, '>': ver > cv, '<=': ver <= cv,
              '<': ver < cv, '==': ver == cv, '!=': ver != cv}[op]
    return ('ok', result)


def satisfies(ver_str, range_str):
    """Tristate satisfaction: True / False / None (None = unevaluable)."""
    ver = parse_semver(ver_str)
    if ver is None:
        return None
    for token in range_str.split():
        kind, value = _eval_token(ver, token, ver_str)
        if kind != 'ok':
            return None
        if not value:
            return False
    return True


def _cli_satisfies(ver_str, range_str):
    """CLI lane: exit-code + stderr contract of the old compat-lint heredoc.

    Token-by-token walk (NOT pre-validate-then-evaluate): '>=9.9.9 badtoken'
    must exit 1 on the first failing constraint before ever seeing the bad
    token -- byte-compatible with the shipped behavior.
    """
    ver = parse_semver(ver_str)
    if ver is None:
        sys.stderr.write("invalid version: %s\n" % ver_str)
        return 2
    for token in range_str.split():
        kind, value = _eval_token(ver, token, ver_str)
        if kind == 'badver':
            sys.stderr.write("invalid version: %s\n" % value)
            return 2
        if kind == 'badtoken':
            sys.stderr.write("unsupported range token: %s\n" % value)
            return 2
        if kind == 'badcv':
            sys.stderr.write("invalid constraint version: %s\n" % value)
            return 2
        if kind == 'badop':
            sys.stderr.write("unsupported operator %s in range\n" % value)
            return 2
        if not value:
            return 1
    return 0


def _cli_compare(a_str, b_str):
    """CLI lane for cmp_precedence: prints -1/0/1, exit 0; exit 2 (stderr
    names the offender) on an unparseable side -- the error lane must never
    read as an ordering."""
    for s in (a_str, b_str):
        if parse_semver_full(s) is None:
            sys.stderr.write("invalid version: %s\n" % s)
            return 2
    print(cmp_precedence(a_str, b_str))
    return 0


def _cli_compare_pack(pack_id, a_str, b_str):
    """compare, legacy-stamp aware for <pack_id> (DGN-2119); same output and
    exit contract as compare."""
    for s in (a_str, b_str):
        if parse_semver_full(s) is None:
            sys.stderr.write("invalid version: %s\n" % s)
            return 2
    print(cmp_pack(pack_id, a_str, b_str))
    return 0


def _main(argv):
    if len(argv) == 5 and argv[1] == "compare-pack":
        return _cli_compare_pack(argv[2], argv[3], argv[4])
    if len(argv) == 4 and argv[1] == "display":
        d = display_version(argv[2], argv[3])
        print(d if d is not None else argv[3].strip())
        return 0
    if len(argv) == 3 and argv[1] == "dev-tail":
        return 0 if (argv[2] in LEGACY_DEV_STAMPS
                     or argv[2] in UNRELEASED_PRODUCTS) else 1
    if len(argv) == 3 and argv[1] == "sort-tags":
        out = sort_tags(argv[2], sys.stdin.read().split())
        if out:
            print("\n".join(out))
        return 0
    if len(argv) == 4 and argv[1] == "pick-tag":
        print(pick_tag(argv[2], argv[3], sys.stdin.read().split()))
        return 0
    if len(argv) == 3 and argv[1] == "is-range":
        return 0 if is_semver_range(argv[2]) else 1
    if len(argv) == 4 and argv[1] == "satisfies":
        return _cli_satisfies(argv[2], argv[3])
    if len(argv) == 4 and argv[1] == "compare":
        return _cli_compare(argv[2], argv[3])
    sys.stderr.write(
        "usage: semver_range.py is-range <string>\n"
        "       semver_range.py satisfies <version> <range>\n"
        "       semver_range.py compare <a> <b>\n"
        "       semver_range.py compare-pack <pack-id> <a> <b>\n"
        "       semver_range.py display <pack-id> <version>\n"
        "       semver_range.py pick-tag <pack-id> <stable|dev>  (tags on stdin)\n"
        "       semver_range.py sort-tags <pack-id>  (tags on stdin)\n"
        "       semver_range.py dev-tail <pack-id>\n")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
