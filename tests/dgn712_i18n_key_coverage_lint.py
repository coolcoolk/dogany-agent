#!/usr/bin/env python3
"""dgn712_i18n_key_coverage_lint.py -- release-content i18n coverage lint.

DGN-712: v1.24.0 bricked drifted live instances because bridge/messages.py
binds i18n strings at MODULE IMPORT time (MODEL_ALREADY_ACTIVE = t(...)). When
a release ships a new module-level t("<key>") reference but its paired key is
missing from a locale catalog, any instance whose i18n did not land the key
crashes at import -> no heartbeat -> watchdog restart loop -> live DOWN.

This lint is the release preflight guard: it scans messages.py for every
module-level t("<key>") / t_strict("<key>") reference and asserts that every
referenced key exists in BOTH i18n/ko.py and i18n/en.py of the SAME bridge
tree. If any referenced key is missing from either catalog, the lint fails
loudly so the broken pairing is never shipped.

Scope: the shippable template bridge (agents/.template/bridge) AND the
canonical instance mirror (agents/main/bridge) -- both must stay green so the
template that lands to instances and the mirror kept in parity are both sound.

Usage:
    python3 tests/dgn712_i18n_key_coverage_lint.py            # lint real tree(s)
    python3 tests/dgn712_i18n_key_coverage_lint.py --selftest # + inject a bad
                                                              #   fixture, assert
                                                              #   the lint flags it

Exit: 0 all referenced keys covered / 1 a referenced key is missing / 2 setup
error (a scanned file was unreadable).

Implementation is deliberately dependency-free: it uses the ast module to read
module-level t()/t_strict() calls in messages.py and the STRINGS dict literals
in en.py/ko.py, so it never imports the bridge package (no config/.env needed)
and runs in any CI shell.
"""

import argparse
import ast
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Bridge trees to lint. The template is what update.sh lands to instances; the
# main mirror is kept in parity. Both must carry sound code<->i18n pairing.
BRIDGE_TREES = [
    REPO_ROOT / "agents" / ".template" / "bridge",
    REPO_ROOT / "agents" / "main" / "bridge",
]

# t() and t_strict() are the resolver entry points that read a catalog key.
RESOLVER_NAMES = {"t", "t_strict"}


def _string_arg(call: ast.Call):
    """Return the literal string first arg of a call, or None if not a literal."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def referenced_keys(messages_py: Path):
    """All i18n keys referenced via t("...")/t_strict("...") in messages.py.

    Scans the whole module (module-level bindings are the crash surface, but a
    key referenced anywhere still needs a catalog entry). Only literal-string
    arguments are collected; a dynamic t(var) is skipped (cannot be linted
    statically and is not the failure mode this guard targets).
    """
    tree = ast.parse(messages_py.read_text(encoding="utf-8"), filename=str(messages_py))
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None
            )
            if name in RESOLVER_NAMES:
                key = _string_arg(node)
                if key is not None:
                    keys.add(key)
    return keys


def catalog_keys(catalog_py: Path):
    """Keys of the top-level STRINGS = {...} dict literal in a locale catalog."""
    tree = ast.parse(catalog_py.read_text(encoding="utf-8"), filename=str(catalog_py))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "STRINGS" in targets and isinstance(node.value, ast.Dict):
                keys = set()
                for k in node.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
                return keys
    raise ValueError(f"no top-level STRINGS dict literal in {catalog_py}")


def lint_tree(bridge_dir: Path):
    """Lint one bridge tree. Returns a list of human-readable violation lines."""
    messages_py = bridge_dir / "messages.py"
    en_py = bridge_dir / "i18n" / "en.py"
    ko_py = bridge_dir / "i18n" / "ko.py"
    for p in (messages_py, en_py, ko_py):
        if not p.is_file():
            raise FileNotFoundError(f"missing bridge file: {p}")

    refs = referenced_keys(messages_py)
    en_keys = catalog_keys(en_py)
    ko_keys = catalog_keys(ko_py)

    violations = []
    for key in sorted(refs):
        missing_in = []
        if key not in en_keys:
            missing_in.append("en")
        if key not in ko_keys:
            missing_in.append("ko")
        if missing_in:
            violations.append(
                f"{_display_path(bridge_dir)}: messages.py references "
                f"i18n key {key!r} missing from: {', '.join(missing_in)}"
            )
    return violations


def _display_path(bridge_dir: Path):
    """Repo-relative path when possible, else the absolute path (fixtures)."""
    try:
        return str(bridge_dir.relative_to(REPO_ROOT))
    except ValueError:
        return str(bridge_dir)


def run_lint(trees):
    """Lint the given trees. Returns 0 if all green, 1 if any violation."""
    all_violations = []
    linted = 0
    for tree in trees:
        if not tree.is_dir():
            # A tree may legitimately not exist in a partial checkout; skip it
            # but keep going so the trees that do exist are still enforced.
            continue
        linted += 1
        all_violations.extend(lint_tree(tree))
    if linted == 0:
        print("i18n coverage lint: no bridge trees found to lint", file=sys.stderr)
        return 2
    if all_violations:
        print("i18n coverage lint FAIL:", file=sys.stderr)
        for v in all_violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print(f"i18n coverage lint OK: {linted} bridge tree(s), all referenced keys covered")
    return 0


def _selftest():
    """Assert both directions: real tree passes; an injected-missing key fails.

    Builds a throwaway bridge fixture by copying the template's messages.py +
    catalogs, deleting one key from BOTH catalogs while keeping its t()
    reference, and asserting the lint flags it. Then asserts the real trees
    pass. Read-only against the repo; fixture lives in a private mktemp dir.
    """
    import shutil

    template_bridge = REPO_ROOT / "agents" / ".template" / "bridge"
    if not template_bridge.is_dir():
        print("selftest FAIL: template bridge not found", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="dgn712-lint-") as tmp:
        fixture = Path(tmp) / "bridge"
        (fixture / "i18n").mkdir(parents=True)
        shutil.copy(template_bridge / "messages.py", fixture / "messages.py")
        # Drop one real key ("model_already_active", the DGN-712 culprit) from
        # BOTH catalogs while the t() reference stays in messages.py.
        drop_key = "model_already_active"
        for name in ("en.py", "ko.py"):
            src_lines = (template_bridge / "i18n" / name).read_text(
                encoding="utf-8"
            ).splitlines(keepends=True)
            kept = [ln for ln in src_lines if f'"{drop_key}"' not in ln]
            (fixture / "i18n" / name).write_text("".join(kept), encoding="utf-8")

        bad = lint_tree(fixture)
        if not any(drop_key in v for v in bad):
            print(
                f"selftest FAIL: lint did not flag missing key {drop_key!r}",
                file=sys.stderr,
            )
            return 1
        print(f"selftest: injected-missing case flagged {drop_key!r} (expected)")

    # Real trees must be clean on the current tree.
    rc = run_lint(BRIDGE_TREES)
    if rc != 0:
        print("selftest FAIL: real tree(s) did not pass the lint", file=sys.stderr)
        return 1
    print("selftest OK: both directions verified")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="inject a bad fixture and assert the lint flags it, then lint real tree(s)",
    )
    args = parser.parse_args()
    if args.selftest:
        sys.exit(_selftest())
    sys.exit(run_lint(BRIDGE_TREES))


if __name__ == "__main__":
    main()
