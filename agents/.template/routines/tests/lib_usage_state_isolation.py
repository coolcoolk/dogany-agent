# test-lane: lib shared helper: per-module tmp usage-state root + cleanliness check (DGN-2168)
"""Point usage-gate.py's state root at a tmp dir for a whole test module and
assert the canonical template's .claude tree was not touched. Subprocesses
inherit the env var, so `source dispatch-detached.sh` cases are covered."""
import os
import shutil
import tempfile

ENV = "DOGANY_USAGE_STATE_DIR"
TEMPLATE_CLAUDE = os.path.realpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".claude"))


def snapshot(root=TEMPLATE_CLAUDE):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
                out[p] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
    return out


class Isolation:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="usage-state-")
        self.prev = os.environ.get(ENV)
        self.before = snapshot()
        os.environ[ENV] = os.path.join(self.tmp, ".claude")

    def close(self):
        if self.prev is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = self.prev
        shutil.rmtree(self.tmp, ignore_errors=True)
        after = snapshot()
        assert after == self.before, (
            "test wrote into the canonical template .claude: %s"
            % sorted(set(after.items()) ^ set(self.before.items())))
