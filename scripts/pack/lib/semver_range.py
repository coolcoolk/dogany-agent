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


def _main(argv):
    if len(argv) == 3 and argv[1] == "is-range":
        return 0 if is_semver_range(argv[2]) else 1
    if len(argv) == 4 and argv[1] == "satisfies":
        return _cli_satisfies(argv[2], argv[3])
    if len(argv) == 4 and argv[1] == "compare":
        return _cli_compare(argv[2], argv[3])
    sys.stderr.write(
        "usage: semver_range.py is-range <string>\n"
        "       semver_range.py satisfies <version> <range>\n"
        "       semver_range.py compare <a> <b>\n")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
