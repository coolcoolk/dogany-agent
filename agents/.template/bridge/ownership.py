"""Born-locked ownership: safe-by-default access control for a fresh bot.

A freshly minted bot with an empty allowed_user_ids list must NOT be wide open.
This module implements a single-precedence ownership resolution plus a one-time
claim flow so the first legitimate user can take ownership via a printed code,
while everyone else is silently ignored.

Precedence (NOT a union):
  1. config.allowed_user_ids non-empty  -> AUTHORITATIVE. Claim mode OFF. The
     listed ids are the owners; owner.lock is ignored entirely.
  2. else owner.lock present (one int)   -> that id is the sole owner.
  3. else .claimed flag present          -> LOCKED OUT (already claimed once;
     lock was removed). Deny all; do NOT reopen claim mode.
  4. else                                -> CLAIM MODE (no owner yet).

State files (plain text under BOT_DATA_DIR):
  owner.lock   -- a single integer Telegram user id (the sole owner)
  claim_code   -- the pending one-time claim code (only in claim mode)
  .claimed     -- sticky marker: this instance was claimed at least once

No integrity crypto: the precedence + sticky rules are the guardrails. The claim
code is generated with the secrets module (CSPRNG), not time/PID seeds.
"""

import logging
import secrets
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

OWNER_LOCK_FILENAME = "owner.lock"
CLAIM_CODE_FILENAME = "claim_code"
CLAIMED_FLAG_FILENAME = ".claimed"

# Unambiguous alphabet: no 0/O, 1/I/L, so a human can read the code off a log
# and type it back without confusion.
CLAIM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CLAIM_CODE_LENGTH = 8

# Resolution modes returned by resolve_owner().
MODE_AUTHORITATIVE = "authoritative"  # allowed_user_ids governs
MODE_OWNER_LOCK = "owner_lock"        # owner.lock governs (owner_id set)
MODE_CLAIM = "claim"                  # no owner yet; claim flow active
MODE_LOCKED_OUT = "locked_out"        # claimed before, lock gone; deny all


def owner_lock_path(bot_data_dir: Path) -> Path:
    return Path(bot_data_dir) / OWNER_LOCK_FILENAME


def claim_code_path(bot_data_dir: Path) -> Path:
    return Path(bot_data_dir) / CLAIM_CODE_FILENAME


def claimed_flag_path(bot_data_dir: Path) -> Path:
    return Path(bot_data_dir) / CLAIMED_FLAG_FILENAME


def read_owner_lock(bot_data_dir: Path) -> Optional[int]:
    """Return the owner id from owner.lock, or None if absent/unreadable.

    A malformed lock (non-integer / empty) is treated as absent rather than
    trusted, so a corrupt file never silently grants access to id 0 etc.
    """
    path = owner_lock_path(bot_data_dir)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("owner.lock unreadable (%s); treating as absent", e)
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("owner.lock content %r is not an integer; treating as absent", raw)
        return None


def is_claimed(bot_data_dir: Path) -> bool:
    return claimed_flag_path(bot_data_dir).exists()


def resolve_owner(
    allowed_user_ids, bot_data_dir: Path
) -> Tuple[str, Optional[int]]:
    """Resolve the effective ownership mode. See module docstring for precedence.

    Returns (mode, owner_id). owner_id is set only for MODE_OWNER_LOCK.
    """
    if allowed_user_ids:
        return (MODE_AUTHORITATIVE, None)
    owner_id = read_owner_lock(bot_data_dir)
    if owner_id is not None:
        return (MODE_OWNER_LOCK, owner_id)
    if is_claimed(bot_data_dir):
        # Sticky anti-reopen: instance was claimed before but owner.lock is gone.
        # Do NOT reopen claim mode on a populated instance; require a manual reclaim.
        return (MODE_LOCKED_OUT, None)
    return (MODE_CLAIM, None)


def generate_claim_code() -> str:
    return "".join(secrets.choice(CLAIM_CODE_ALPHABET) for _ in range(CLAIM_CODE_LENGTH))


def read_claim_code(bot_data_dir: Path) -> Optional[str]:
    path = claim_code_path(bot_data_dir)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("claim_code unreadable (%s)", e)
        return None
    return raw or None


def ensure_claim_code(bot_data_dir: Path) -> str:
    """Return the pending claim code, generating and persisting one if absent.

    Reuses an existing claim_code file so a restart does not rotate the code
    (which would strand a user mid-claim). Only called while in claim mode.
    """
    existing = read_claim_code(bot_data_dir)
    if existing:
        return existing
    code = generate_claim_code()
    path = claim_code_path(bot_data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically-ish: exclusive-create loses a race to a concurrent writer,
    # in which case we re-read the winner's code rather than overwrite it.
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(code)
        return code
    except FileExistsError:
        winner = read_claim_code(bot_data_dir)
        return winner or code


def parse_claim_command(text: str) -> Optional[str]:
    """If text is exactly '/claim <code>' (single token code), return the code.

    Returns None for anything else (including bare '/claim', extra args, or a
    command addressed to another bot like '/claim@x'). Case of the code is
    preserved; the alphabet is uppercase so verification is exact-match.
    """
    if not text:
        return None
    stripped = text.strip()
    parts = stripped.split()
    if len(parts) != 2:
        return None
    head = parts[0]
    # Accept '/claim' and '/claim@botname' (Telegram appends @bot in groups).
    if head != "/claim" and not head.startswith("/claim@"):
        return None
    return parts[1]


def verify_and_claim(
    text: str, user_id: int, bot_data_dir: Path
) -> bool:
    """Attempt a claim from message `text` by `user_id`.

    On a correct '/claim <code>': write owner.lock, set the sticky .claimed flag,
    delete claim_code, and return True. Any mismatch (not a claim command, wrong
    code) returns False and changes nothing. The caller stays silent regardless.
    """
    supplied = parse_claim_command(text)
    if supplied is None:
        return False
    expected = read_claim_code(bot_data_dir)
    if not expected:
        return False
    # Constant-time compare to avoid leaking code length/prefix via timing.
    if not secrets.compare_digest(supplied, expected):
        return False
    _write_owner(user_id, bot_data_dir)
    return True


def _write_owner(user_id: int, bot_data_dir: Path) -> None:
    data_dir = Path(bot_data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    owner_lock_path(data_dir).write_text(str(int(user_id)), encoding="utf-8")
    # Sticky: mark claimed so the instance can never silently reopen claim mode.
    claimed_flag_path(data_dir).write_text("1", encoding="utf-8")
    # Consume the one-time code so it cannot be replayed.
    try:
        claim_code_path(data_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("failed to delete claim_code after claim: %s", e)
    logger.info("bot claimed by user id %s; owner.lock written, claim mode closed", user_id)
