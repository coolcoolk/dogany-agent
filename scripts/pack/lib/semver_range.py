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
#   behavior of both prior copies byte-for-byte.
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
#   Anything else: usage on stderr, exit 2 (error lane -- a misuse must
#   never read as "satisfied" or "valid range").
#
# Library:
#   parse_semver(s)        -> (major, minor, patch) or None
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
# No end anchor after the numeric triple: '>=1.2.3-rc' is a valid token and
# its constraint version prefix-parses to (1,2,3) (shipped behavior of both
# prior copies -- kept).
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


def is_semver_range(s):
    """True iff s is 1+ whitespace-separated strict constraint tokens."""
    tokens = s.split()
    if not tokens:
        return False
    return all(_VALID_OP.match(t) for t in tokens)


def _eval_token(ver, token):
    """Evaluate one constraint token against a parsed version tuple.

    Returns a pair (kind, value):
      ('ok', bool)          -- constraint evaluated; bool = holds?
      ('badtoken', token)   -- token matches no known operator shape
      ('badcv', cv_str)     -- operator ok, constraint version unparseable
      ('badop', op)         -- ^ / ~: recognized but unsupported
    Check order (badtoken -> badcv -> badop) mirrors the old compat-lint
    heredoc so CLI diagnostics stay byte-identical.
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
    result = {'>=': ver >= cv, '>': ver > cv, '<=': ver <= cv,
              '<': ver < cv, '==': ver == cv, '!=': ver != cv}[op]
    return ('ok', result)


def satisfies(ver_str, range_str):
    """Tristate satisfaction: True / False / None (None = unevaluable)."""
    ver = parse_semver(ver_str)
    if ver is None:
        return None
    for token in range_str.split():
        kind, value = _eval_token(ver, token)
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
        kind, value = _eval_token(ver, token)
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


def _main(argv):
    if len(argv) == 3 and argv[1] == "is-range":
        return 0 if is_semver_range(argv[2]) else 1
    if len(argv) == 4 and argv[1] == "satisfies":
        return _cli_satisfies(argv[2], argv[3])
    sys.stderr.write(
        "usage: semver_range.py is-range <string>\n"
        "       semver_range.py satisfies <version> <range>\n")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
