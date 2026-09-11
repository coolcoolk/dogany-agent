"""Host-root probes for bridge tests that assert on files OUTSIDE bridge/.

A handful of tests verify the bridge's contract with its HOST -- the instance
root that the bridge package sits inside: `vendors/telegram.md` (vendor
contract), `routines/push.sh` (outbound hop), `routines/lib/machine_signal.py`
(emitter half of the machine-line gate). Those files belong to the host, not
to the bridge, and the public distribution ships `bridge/` ALONE (DGN-1276:
the generated tree is a subtree, and the OSS repo's own root carries neither
`vendors/` nor `routines/lib/`). Measured before this helper existed: an OSS
consumer running the shipped suite got 20 failures whose only cause was
"this checkout is not an instance root".

A failure there is a false alarm -- nothing is broken, the host half simply
does not exist in that distribution. So those tests SKIP when the host file is
absent.

The obvious hole in "skip when absent" is that deleting the host file in the
CANONICAL repo would also silence the test, which is exactly the regression it
was written to catch. So absence alone is not enough to skip: the tree must
also fail to look like an instance root at all. `config/agent.conf` is the
marker -- every instance and the canonical template carry it, no consumer
distribution does. In an instance root a missing host file still FAILS.

Usage (both styles, because the callers are split):

    from bridge.tests._hostroot import host_file, skip_without_host  # unittest
    @unittest.skipUnless(*skip_without_host("vendors/telegram.md"))

    from bridge.tests._hostroot import requires_host                 # pytest
    @requires_host("routines/lib/machine_signal.py")
"""

from pathlib import Path

# bridge/tests/_hostroot.py -> bridge/tests -> bridge -> the host root
HOST_ROOT = Path(__file__).resolve().parents[2]


def host_file(rel: str) -> Path:
    return HOST_ROOT / rel


def is_instance_root() -> bool:
    """True when this tree is an agent instance (or the canonical template)."""
    return (HOST_ROOT / "config" / "agent.conf").exists()


def host_available(rel: str) -> bool:
    """False only when the host file is absent AND this is no instance root."""
    return host_file(rel).exists() or is_instance_root()


def skip_reason(rel: str) -> str:
    return (f"host file {rel!r} absent and {HOST_ROOT} is not an instance root "
            f"(the public distribution ships bridge/ alone) -- nothing to assert")


def skip_without_host(rel: str):
    """(condition, reason) pair for unittest.skipUnless."""
    return host_available(rel), skip_reason(rel)


def requires_host(rel: str):
    """pytest decorator form of the same predicate."""
    import pytest
    return pytest.mark.skipif(not host_available(rel), reason=skip_reason(rel))
