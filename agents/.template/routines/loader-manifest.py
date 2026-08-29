#!/usr/bin/env python3
"""loader-manifest.py -- SessionStart hook: make hot-doc loading observable.

DGN-1142 section 3.1 (wired as part of DGN-1141 stage 4). The @ include chain
(CLAUDE.md -> AGENTS.md -> baseline docs) fails in COMPLETE silence: a missing
@-referenced file produces exit 0, zero stderr, zero log (measured,
DGN-1141-M2 section 2). Until this hook, "loaded 0 docs" and "the loader never
ran" were the same observation: none.

This hook statically walks the @ graph from CLAUDE.md (standalone `@path`
lines outside fenced code blocks, resolved relative to the referencing file,
recursively) and emits ONE line as additionalContext + appends the same line
to .telegram_bot/state/loader-manifest.log:

    [loader] 4/4 loaded: AGENTS,hot.framework,hot.custom.agent,hot.custom.owner
    [loader] 3/4 loaded; MISSING: hot.framework.md
    [loader] 0/0 loaded: (no @-references in CLAUDE.md)
    [loader] 0/1 loaded; MISSING: CLAUDE.md

Three-point output contract (DGN-1142 3.1: verdict, population, tool
liveness): the N/M count is the population; the file names are the verdict
detail; the line's very existence is the liveness evidence. State meanings:
  - no line at all        -> the hook itself never ran (settings wiring dead)
  - N/M with MISSING      -> the loader WILL silently skip the named files
  - N/M all loaded        -> every referenced doc exists on disk
This is a static existence claim (stat), not proof the runtime consumed the
bytes -- the runtime offers no such signal to observe (measured above).

Known blind spot: inline `... see @foo.md ...` imports are not counted --
the estate convention is standalone @ lines and matching inline tokens would
false-positive on decorators/emails in fenced-adjacent prose.

Fail-open for the SESSION (never blocks, always exit 0), fail-LOUD for the
observation (any internal error still tries to emit a "[loader] ERROR: ..."
line so the failure is a line, not an absence).
"""
import json
import os
import re
import sys
import time

# A standalone @-reference line: the whole (stripped) line is "@<path>" with
# no spaces in the path. This is the estate authoring convention for the hot
# include chain (CLAUDE.md / AGENTS.md use it).
_REF_RE = re.compile(r"^@(\S+)$")


def _refs_outside_fences(text):
    """Yield @-reference path strings from standalone lines outside ``` fences."""
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _REF_RE.match(stripped)
        if m:
            yield m.group(1)


def walk_at_graph(root_dir):
    """Statically walk the @ graph from <root_dir>/CLAUDE.md.

    Returns (loaded, missing): lists of file basenames in discovery order.
    A missing file cannot be recursed into, so its own children (if any) are
    unknown -- exactly mirroring what the real loader would skip.
    """
    root = os.path.join(root_dir, "CLAUDE.md")
    if not os.path.isfile(root):
        return [], ["CLAUDE.md"]
    loaded, missing = [], []
    seen = {os.path.realpath(root)}
    queue = [root]
    while queue:
        current = queue.pop(0)
        try:
            with open(current, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        base_dir = os.path.dirname(current)
        for ref in _refs_outside_fences(text):
            path = ref if os.path.isabs(ref) else os.path.join(base_dir, ref)
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            name = os.path.basename(ref)
            if os.path.isfile(path):
                loaded.append(name)
                queue.append(path)
            else:
                missing.append(name)
    return loaded, missing


def build_line(loaded, missing):
    n, m = len(loaded), len(loaded) + len(missing)
    if missing:
        return "[loader] {}/{} loaded; MISSING: {}".format(
            n, m, ",".join(missing)
        )
    if not loaded:
        return "[loader] 0/0 loaded: (no @-references in CLAUDE.md)"
    names = ",".join(
        name[:-3] if name.endswith(".md") else name for name in loaded
    )
    return "[loader] {}/{} loaded: {}".format(n, m, names)


def _append_log(root_dir, line):
    try:
        state_dir = os.path.join(root_dir, ".telegram_bot", "state")
        os.makedirs(state_dir, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(
            os.path.join(state_dir, "loader-manifest.log"), "a", encoding="utf-8"
        ) as f:
            f.write("{} {}\n".format(stamp, line))
    except Exception:
        pass  # log append is best-effort; the additionalContext line is primary


def main():
    try:
        sys.stdin.read()  # consume the SessionStart payload so the pipe closes
    except Exception:
        pass
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        loaded, missing = walk_at_graph(root_dir)
        line = build_line(loaded, missing)
    except Exception as e:  # noqa: BLE001 -- a failure must be a LINE, not silence
        line = "[loader] ERROR: manifest walk failed ({})".format(e)
    _append_log(root_dir, line)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": line,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
