"""Tool permission policy: AskUserQuestion degradation + out-of-root path guard.

The bot wires these into the SDK's can_use_tool callback. AskUserQuestion is
force-denied (degraded to numbered text). File/Bash tools touching paths outside
PROJECT_ROOT are denied once, with a numbered allow/deny prompt for the user;
an explicit approval grants a single subsequent pass.
"""

import shlex
from pathlib import Path
from typing import Any, Iterable, List

from bridge import messages

PATH_GUARDED_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "Bash"}
PATH_KEYWORDS = ("path", "file", "cwd", "dir", "directory", "root")
ALLOW_OUTSIDE_ONCE_TOKEN = "ALLOW_OUTSIDE_ONCE"
DENY_OUTSIDE_TOKEN = "DENY_OUTSIDE"


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _paths_from_command(command: str) -> List[str]:
    try:
        tokens = shlex.split(command)
    except Exception:
        tokens = command.split()
    out: List[str] = []
    for token in tokens:
        token = token.strip()
        if not token or token.startswith("-") or "://" in token:
            continue
        if token.startswith(("~", "/", "./", "../")) or "/" in token:
            out.append(token)
    return out


def _resolve_candidate(raw: str, project_root: Path) -> Path:
    candidate = Path(raw.strip().strip("\"'")).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve(strict=False)


def _is_within_root(path: Path, project_root: Path) -> bool:
    try:
        return path.is_relative_to(project_root)
    except Exception:
        return False


def extract_path_candidates(tool_name: str, tool_input: Any) -> List[str]:
    candidates: List[str] = []
    seen = set()

    def add(raw: str) -> None:
        raw = raw.strip()
        if raw and raw not in seen:
            seen.add(raw)
            candidates.append(raw)

    def walk(value: Any, parent_key: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_lower = key.lower()
                if isinstance(item, str) and any(w in key_lower for w in PATH_KEYWORDS):
                    add(item)
                else:
                    walk(item, key_lower)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, parent_key)
        elif isinstance(value, str) and parent_key == "command":
            for token in _paths_from_command(value):
                add(token)

    walk(tool_input)
    if tool_name == "Bash":
        for text in _iter_strings(tool_input):
            for token in _paths_from_command(text):
                add(token)
    return candidates


def extract_outside_paths(tool_name: str, tool_input: Any, project_root: Path) -> List[str]:
    """Return resolved paths that fall outside project_root for guarded tools."""
    if tool_name not in PATH_GUARDED_TOOLS:
        return []
    outside: List[str] = []
    seen = set()
    for raw in extract_path_candidates(tool_name, tool_input):
        try:
            resolved = _resolve_candidate(raw, project_root)
        except Exception:
            continue
        if not _is_within_root(resolved, project_root):
            path_str = str(resolved)
            if path_str not in seen:
                seen.add(path_str)
                outside.append(path_str)
    return outside


def outside_path_deny_message(outside_paths: List[str]) -> str:
    preview = "\n".join(f"- {p}" for p in outside_paths[:5])
    return messages.OUTSIDE_PATH_DENY.format(
        preview=preview,
        allow_token=ALLOW_OUTSIDE_ONCE_TOKEN,
        deny_token=DENY_OUTSIDE_TOKEN,
    )
