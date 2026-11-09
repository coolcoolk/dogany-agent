"""DGN-2147: a contended bot token, named in plain words.

Two bridges polling one bot token take getUpdates from each other (Telegram
answers 409 Conflict). The bridge keeps cycling Conflict -> stop -> re-init;
a message that lands in the gap used to hit a half-torn application. This
module holds the two pure pieces of the answer: finding another LOCAL process
that holds the same token (so the notice can name it), and the once-per-streak
latch that decides when the owner is told. Stdlib only.
"""

import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bridge import instance_lock

# A Conflict streak older than this tells the owner (seconds). Short streaks
# are the normal canary-cutover blip and stay silent.
NOTICE_AFTER_S = 180


def _env_token(root: Path) -> Optional[str]:
    for rel in (".telegram_bot/.env", "runtime/.env"):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return "".join(line.split("=", 1)[1].split()).strip("\"'") or None
    return None


# ESTATE:BEGIN sibling-instance discovery reads the Dogany estate layout (~/.dogany registry, agents/ and dogany/ trees); the public build knows no sibling instances (DGN-2147)
def estate_roots(home: Optional[Path] = None) -> List[Path]:
    """Candidate instance roots on this machine (registry + fixed-depth scan)."""
    home = Path(home) if home else Path.home()
    found: Dict[str, Path] = {}

    def add(p: Path) -> None:
        if (p / ".instance.conf").is_file():
            found.setdefault(str(p.resolve()), p)

    try:
        for line in (home / ".dogany" / "instances").read_text(encoding="utf-8").splitlines():
            if "\t" in line:
                add(Path(line.split("\t", 1)[1].strip()))
    except OSError:
        pass
    for pattern in (".dogany/main", ".dogany/agents/*", "dogany/*",
                    "dogany/*/agents/*", "dogany/*/poc/*"):
        for p in home.glob(pattern):
            add(p)
    return list(found.values())
# ESTATE:ELSE
# def estate_roots(home: Optional[Path] = None) -> List[Path]:
#     """A public single install has no sibling instances to find."""
#     return []
# ESTATE:END


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def local_holders(token: str, own_root, roots: Optional[List[Path]] = None,
                  alive: Callable[[object], bool] = _alive) -> List[str]:
    """Names of OTHER local agents configured with `token`.

    A name is returned when that agent's bridge process is running (its
    instance lock records a live pid); a configured-but-stopped agent is not
    polling and is not a holder."""
    if not token:
        return []
    own = Path(own_root).resolve()
    names: List[str] = []
    for root in roots if roots is not None else estate_roots():
        if root.resolve() == own or _env_token(root) != token:
            continue
        holder = instance_lock.read_holder(root)
        if holder and alive(holder.get("pid")):
            names.append(root.resolve().name)
    return names


class ConflictNotice:
    """Fires once per Conflict streak, after NOTICE_AFTER_S of unresolved
    contention; `recovered()` re-arms it."""

    def __init__(self, after_s: float = NOTICE_AFTER_S, clock=time.time):
        self.after_s, self.clock = after_s, clock
        self.since: Optional[float] = None
        self.sent = False

    def conflict(self) -> bool:
        """Record one Conflict; True exactly once per streak, when due."""
        now = self.clock()
        if self.since is None:
            self.since = now
        if not self.sent and now - self.since >= self.after_s:
            self.sent = True
            return True
        return False

    def recovered(self) -> None:
        self.since, self.sent = None, False
