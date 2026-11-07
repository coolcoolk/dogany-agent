#!/usr/bin/env python3
"""Local job admission and FIFO replay, shared by ordinary agents.

Only resource exclusion, durable requests and job ownership live here.
Developer queue priorities, replacement, publication and repository policy
belong to an explicitly selected host driver.
"""
import argparse
import contextlib
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

class Unavailable(Exception):
    """The host cannot safely decide."""

LOCAL_RUNTIMES = ('claude-code', 'codex', 'claude-api', 'script')
REQUEST_SCHEMA = 'local-job/1'


SCHEMA = "1"

LOCK_WAIT_S = 10.0           # past this -> degraded mode (design 8)

ACCT_ENV_KEYS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME")

LOAD_PER_NCPU = 1.5

LOAD_PER_WORKER = 1.0

MEM_PER_WORKER_MB = 512

MEM_RESERVE_MB = 1024

YOUNG_LEASE_S = 300          # leases younger than this are not in load5 yet

SWAP_HOLD_MB = 4096          # swap in use past this ...

SWAP_HOLD_LOAD_FRAC = 0.75   # ... while load5 >= this share of the limit -> no room

SWAP_HARD_MB = 8192          # swap past this closes the host to EVERY local run, any load

LLM_LOAD_PER_WORKER = 0.1    # an LLM-bound run (no local build/test) is mostly waiting on the network

LLM_MEM_PER_WORKER_MB = 256

RUN_KINDS = ("build", "llm")  # build = local build/test declared (or unknown); llm = none

HEAVY_YOUNG_S = 300          # a heavy-test slot this young is charged ...

HEAVY_LOAD_FRAC = 0.5        # ... at ncpu * this load (a sweep runs ncpu/2 jobs)

def shared_home():
    return os.environ.get("DOGANY_SHARED_HOME") or os.path.join(os.path.expanduser("~"), ".dogany")

PINNED_CLOCK_ENV = ("DOGANY_DISPATCH_NOW", "DOGANY_DISPATCH_MONO", "DOGANY_DISPATCH_BOOT")

def _real_state_dir():
    try:
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:  # noqa: BLE001 -- no passwd entry: fall back to $HOME
        home = os.path.expanduser("~")
    return os.path.realpath(os.path.join(home, ".dogany", "dispatcher"))

def root():
    r = os.path.join(shared_home(), "dispatcher")
    # DGN-2142 guard: a pinned (test) clock must never write the real host
    # state -- its tiny monotonic base poisons clock.json for the live daemon.
    pinned = [k for k in PINNED_CLOCK_ENV if os.environ.get(k)]
    if (pinned or os.environ.get("PYTEST_CURRENT_TEST")) and os.path.realpath(r) == _real_state_dir():
        raise RuntimeError("refusing the real dispatcher state dir under a pinned/test clock (%s); "
                           "set DOGANY_SHARED_HOME to a temp dir" % ",".join(pinned or ["pytest"]))
    return r

def _p(*parts):
    return os.path.join(root(), *parts)

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default

def write_json(path, obj):
    """tmp + fsync + rename: a reader sees old or new, never torn."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.%d.%s" % (os.path.basename(path), os.getpid(), uuid.uuid4().hex[:6]))
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def append_line(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, sort_keys=True) + "\n")

def read_lines(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a torn append is skipped (design 14.4)
    except OSError:
        pass
    return out

def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass

@contextlib.contextmanager
def host_lock(wait=LOCK_WAIT_S):
    """Exclusive flock on the host lock. Python opens files non-inheritable
    (close-on-exec), so a spawned worker never keeps the lock (14.3)."""
    try:
        os.makedirs(root(), exist_ok=True)
        fd = os.open(_p("lock"), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    except OSError as e:
        raise Unavailable("state dir unwritable: %s" % e)
    t0 = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise Unavailable("lock failed: %s" % e)
                if time.monotonic() - t0 >= wait:
                    raise Unavailable("lock wait > %.0fs" % wait)
                time.sleep(0.02)
        ver = _p("VERSION")
        if read_text(ver) != SCHEMA:
            with open(ver, "w") as f:
                f.write(SCHEMA)
        yield
    finally:
        os.close(fd)  # closing drops the flock

def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""

def wall_now():
    v = os.environ.get("DOGANY_DISPATCH_NOW", "")
    return float(v) if v else time.time()

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError):
        return True   # EPERM / a doubt keeps the holder alive
    return True

def machine_read():
    """(load5, ncpu, avail MB, swap used MB) or None. Read outside the lock.
    DOGANY_DISPATCH_MACHINE="load5 ncpu avail_mb swap_mb" pins it (tests)."""
    v = os.environ.get("DOGANY_DISPATCH_MACHINE", "")
    if v:
        try:
            a = [float(x) for x in v.split()]
            return a[0], int(a[1]), a[2], a[3] if len(a) > 3 else 0.0
        except (ValueError, IndexError):
            return None
    try:
        load5 = os.getloadavg()[1]
        ncpu = os.cpu_count() or 1
        if sys.platform == "darwin":
            def sysctl(k):
                return subprocess.run(["sysctl", "-n", k], capture_output=True,
                                      text=True, timeout=5).stdout.strip()
            avail = int(sysctl("hw.memsize")) * int(sysctl("kern.memorystatus_level")) / 100 / 2 ** 20
            m = re.search(r"used = ([0-9.]+)([MG])", sysctl("vm.swapusage"))
            swap = float(m.group(1)) * (1024 if m and m.group(2) == "G" else 1) if m else 0.0
        else:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, rest = line.partition(":")
                    info[k] = int(rest.split()[0]) / 1024
            avail = info["MemAvailable"]
            swap = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
        return load5, ncpu, avail, swap
    except Exception:  # noqa: BLE001
        return None

def heavy_slot():
    """(holder pid, age s) of the machine-wide heavy-test slot, or None."""
    d = os.path.join(shared_home(), "cron-queue", "heavy-test", "slot-1")
    pid = read_text(os.path.join(d, "pid"))
    if not pid.isdigit() or not pid_alive(pid):
        return None
    try:
        age = wall_now() - os.stat(d).st_mtime
    except OSError:
        age = 0.0
    return int(pid), max(0.0, age)

def machine_room(st, mach, hold="", kind="build"):
    """(room, note): how many MORE local workers of `kind` the host takes now.
    An llm run (no local build/test) is charged little CPU and is not held by
    load5 or an explicit resource hold; the swap ceiling and a memory floor close every kind."""
    if kind == "llm":
        if mach is None:
            return 10 ** 6, "machine read FAILED (no load/memory bound applied)"
        load5, ncpu, avail, swap = mach
        young = [l for l in st["leases"].values()
                 if is_local(l.get("lane", "")) and wall_now() - float(l.get("granted_wall", 0)) < YOUNG_LEASE_S]
        mem = avail - sum(LLM_MEM_PER_WORKER_MB if l.get("kind") == "llm" else MEM_PER_WORKER_MB for l in young)
        room = max(0, math.floor((mem - MEM_RESERVE_MB) / LLM_MEM_PER_WORKER_MB))
        if swap >= SWAP_HARD_MB:
            room = 0
        return room, ("machine(llm run) load5=%.1f ignored mem_avail=%dMB swap=%dMB (hard %d) -> room %d" % (
            load5, avail, swap, SWAP_HARD_MB, room))
    if hold:
        return 0, "machine HOLD: " + hold
    if mach is None:
        return 10 ** 6, "machine read FAILED (no load/memory bound applied)"
    load5, ncpu, avail, swap = mach
    now = wall_now()
    young = [l for l in st["leases"].values()
             if is_local(l.get("lane", "")) and now - float(l.get("granted_wall", 0)) < YOUNG_LEASE_S]
    limit = ncpu * LOAD_PER_NCPU
    load = load5 + sum(LLM_LOAD_PER_WORKER if l.get("kind") == "llm" else LOAD_PER_WORKER for l in young)
    mem = avail - sum(LLM_MEM_PER_WORKER_MB if l.get("kind") == "llm" else MEM_PER_WORKER_MB for l in young)
    extra = ""
    hs = heavy_slot()
    if hs and hs[1] < HEAVY_YOUNG_S:
        load += ncpu * HEAVY_LOAD_FRAC
        extra = " heavy-test young (+%.1f load)" % (ncpu * HEAVY_LOAD_FRAC)
    by_load = max(0, math.floor((limit - load) / LOAD_PER_WORKER))
    by_mem = max(0, math.floor((mem - MEM_RESERVE_MB) / MEM_PER_WORKER_MB))
    room = min(by_load, by_mem)
    swap_note = ""
    if swap >= SWAP_HARD_MB or (swap >= SWAP_HOLD_MB and load5 >= limit * SWAP_HOLD_LOAD_FRAC):
        room = 0
        swap_note = " swap=%dMB>=%d under load -> 0" % (swap, SWAP_HOLD_MB)
    local = sum(1 for l in st["leases"].values() if is_local(l.get("lane", "")))
    return room, ("machine load5=%.1f+%d young/%g (+%d) mem_avail=%dMB (+%d) swap=%dMB%s%s "
                  "host local leases=%d -> room %d" % (load5, len(young), limit, by_load, avail,
                                                        by_mem, swap, extra, swap_note, local, room))

def account_for(runtime, api_dir="", env=None):
    """The lane account: the fingerprint the host books already use. `env`
    (a ticket's CLAUDE_CONFIG_DIR / CODEX_HOME) re-resolves it for relane."""
    if runtime == "claude-api":
        if api_dir:
            return os.path.basename(api_dir.rstrip("/"))
        try:
            import book_identity
            return book_identity.current_api_fp() or "unknown"
        except Exception:  # noqa: BLE001
            return "unknown"
    if runtime == "script":
        return "local"   # DGN-2115: a script job spends no provider account
    try:
        import reset_passes as rp
        import api_credit_pool as pool
        e = os.environ if env is None else dict(os.environ, **env)
        if env is not None:
            for k in ACCT_ENV_KEYS:
                if k not in env:
                    e.pop(k, None)
        aid = rp.account_id("claude" if runtime == "claude-code" else runtime, env=e)
        return pool.key_fingerprint(aid) if aid else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"

def _run_env(path):
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                k, eq, v = line.rstrip("\n").partition("=")
                if eq and re.match(r"^[A-Z_][A-Z0-9_]*$", k):
                    try:
                        import shlex
                        p = shlex.split(v)
                        out[k] = p[0] if p else ""
                    except ValueError:
                        out[k] = v
    except OSError:
        pass
    return out

def _parse_dispatch_time(value):
    """Parse dispatch ISO timestamps without depending on the host locale."""
    if not isinstance(value, str) or not value:
        return None
    try:
        import datetime
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (TypeError, ValueError, OverflowError):
        return None

def _display(value):
    """Make filesystem names printable even when they contain raw bytes."""
    return os.fsencode(value).decode("utf-8", "replace")

def scratch_root():
    """Same rule as run-tests.sh (DGN-1979): the host's top-level runs."""
    return os.environ.get("DOGANY_TEST_SCRATCH") or \
        os.path.join(os.path.expanduser("~"), ".dogany", "scratch", "tests")

def cmd_reap_scratch(a):
    """Remove run-tests roots under the per-host scratch root whose owner pid
    is gone (or with no owner.pid past the floor). Same rule as run-tests'
    own sweep (DGN-1896); this is the cleanup routine's pass."""
    base = scratch_root()
    floor = float(a.min_age_h) * 3600
    n = 0
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        d = os.path.join(base, name)
        if not name.startswith("run-tests-disc.") or os.path.islink(d) or not os.path.isdir(d):
            continue
        age = wall_now() - os.stat(d).st_mtime
        owner = read_text(os.path.join(d, "owner.pid"))
        if owner.isdigit():
            if pid_alive(owner):
                continue
        elif age < floor:
            continue
        if a.apply:
            subprocess.run(["chmod", "-R", "u+w", d], capture_output=True)
            shutil.rmtree(d, ignore_errors=True)
        n += 1
        print("%s\t%s" % ("removed" if a.apply else "would-remove", d))
    print("scratch: %d %s under %s" % (n, "removed" if a.apply else "removable", base))
    n = _reap_system_tmp(a, float(a.tmp_min_age_h) * 3600)
    print("system-tmp: %d %s under %s" %
          (n, "removed" if a.apply else "removable", a.tmpdir or tempfile.gettempdir()))
    return 0

TMP_SWEEP_PREFIXES = ("session-config-install-", "test-pack-lanes-",
                      "update-apply-notices-", "run-suite.", "ledger-rcv-",
                      "dogany-merge-gate-", "run-tests-disc.")

def _reap_system_tmp(a, floor):
    """Reap only allowlisted, old test roots with no live owner pid."""
    base = a.tmpdir or tempfile.gettempdir()
    count = 0
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        path = os.path.join(base, name)
        if (not name.startswith(TMP_SWEEP_PREFIXES) or os.path.islink(path) or
                not os.path.isdir(path)):
            continue
        try:
            if wall_now() - os.stat(path).st_mtime < floor:
                continue
        except OSError:
            continue
        owner = read_text(os.path.join(path, "owner.pid"))
        if owner.isdigit() and pid_alive(owner):
            continue
        if a.apply:
            subprocess.run(["chmod", "-R", "u+w", path], capture_output=True)
            shutil.rmtree(path, ignore_errors=True)
        count += 1
        print("%s\t%s" % ("removed" if a.apply else "would-remove", _display(path)))
    return count

def is_local(lane):
    return lane.partition('@')[0] in LOCAL_RUNTIMES


def load_state():
    state = {key: {} for key in ('leases', 'queue')}
    for key in state:
        for path in Path(_p(key)).glob('*.json'):
            value = read_json(str(path))
            if not isinstance(value, dict) or not value.get('id'):
                raise Unavailable('invalid host record: ' + str(path))
            state[key][value['id']] = value
    state['lanes'] = read_json(_p('lanes.json'), {})
    return state


def _save(kind, row):
    write_json(_p(kind, row['id'] + '.json'), row)


def _reap(state):
    for key, lease in list(state['leases'].items()):
        if lease.get('request_schema') != REQUEST_SCHEMA:
            continue  # unowned historical leases require their adapter's migration
        marker = lease.get('marker', '')
        finished = marker and os.path.isfile(os.path.join(marker, '.finalized/complete'))
        pid = lease.get('pid', 0)
        dead = pid and not pid_alive(pid)
        abandoned = not pid and wall_now() - lease.get('granted_wall', wall_now()) > 120
        if finished or dead or abandoned:
            if abandoned and lease.get('request_schema') == REQUEST_SCHEMA and lease.get('ticket_doc'):
                _save('queue', lease['ticket_doc'])
            _unlink(_p('leases', key + '.json'))
            del state['leases'][key]


def _lane(args):
    if args.runtime not in LOCAL_RUNTIMES:
        raise Unavailable('runtime requires an explicitly selected host driver')
    return args.runtime + '@' + args.account


def _refresh(state, args):
    lane = _lane(args)
    if args.cap is not None:
        if args.cap < 0:
            raise Unavailable('negative runtime cap')
        state['lanes'][lane] = dict(cap=args.cap, at=wall_now(), note=args.cap_note)
        write_json(_p('lanes.json'), state['lanes'])
    return lane


def _fits(state, lane, machine, kind):
    cap = state['lanes'].get(lane, {})
    if wall_now() - cap.get('at', 0) > 900:
        return False, 'runtime capacity snapshot is missing or stale'
    active = sum(l.get('lane') == lane for l in state['leases'].values())
    if active >= cap.get('cap', 0):
        return False, 'runtime at capacity'
    room, note = machine_room(state, machine, kind=kind)
    return room > 0, note


def _grant(state, request, pid=0):
    ident = 'lease-' + uuid.uuid4().hex[:16]
    lease = dict(id=ident, agent=request['agent'], lane=request['lane'], pid=pid,
                 runid=request.get('runid', ''), marker='', kind=request['kind'],
                 granted_wall=wall_now(), request_schema=REQUEST_SCHEMA)
    if not pid:
        lease['ticket_doc'] = request
    _save('leases', lease)
    state['leases'][ident] = lease
    return lease


def cmd_acquire(args):
    machine = machine_read()
    with host_lock():
        state = load_state()
        _reap(state)
        lane = _refresh(state, args)
        if args.lease:
            lease = state['leases'].get(args.lease)
            if not lease or lease.get('agent') != args.agent or lease.get('lane') != lane:
                return 'blocked', '-', 'queued lease ownership mismatch'
            # A drained lease occupies its slot already; recheck the client cap
            # at execution without charging this lease a second time.
            lease.update(pid=args.pid, runid=args.runid or lease.get('runid', ''))
            _save('leases', lease)
            return 'granted', lease['id'], 'adopted queued lease'
        if any(q.get('request_schema') == REQUEST_SCHEMA and q.get('lane') == lane
               for q in state['queue'].values()) and not args.force:
            return 'wait', '-', 'older request queued'
        ok, note = _fits(state, lane, machine, args.kind)
        if not ok and not args.force:
            return 'wait', '-', note
        req = dict(agent=args.agent, lane=lane, runid=args.runid, kind=args.kind)
        lease = _grant(state, req, args.pid)
        return 'granted', lease['id'], note


def cmd_enqueue(args):
    replay = read_json(args.argv_file)
    if not isinstance(replay, dict) or not isinstance(replay.get('argv'), list):
        raise Unavailable('persisted replay argv is unavailable')
    if not os.path.isabs(args.script) or not os.access(args.script, os.X_OK):
        raise Unavailable('request script must be an absolute executable')
    route = replay.get('return_route')
    if route:
        from return_route import validate_route
        route = validate_route(route)
    with host_lock():
        state = load_state()
        lane = _refresh(state, args)
        # Identical owner/argv is one request even after a caller loses its reply.
        for row in state['queue'].values():
            if (row.get('request_schema') == REQUEST_SCHEMA and row.get('agent') == args.agent
                    and row.get('argv') == replay['argv']):
                return 'queued', row['id'], 'already queued'
        ident = 'request-' + uuid.uuid4().hex[:16]
        env = {k: v for k, v in os.environ.items()
               if k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'TMPDIR',
                        'DOGANY_SHARED_HOME', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME',
                        'DOGANY_RECORD_PROVIDER', 'DOGANY_PM_SEAT_RESOLVER',
                        'DOGANY_JOB_DRIVER', 'DOGANY_HOST_DRIVER', 'DOGANY_FRAMEWORK_ROOT',
                        'DOGANY_INSTANCE_ROOT', 'DOGANY_DEVKIT_LIB', 'DOGANY_DEVKIT_CMD',
                        'DOGANY_DISPATCH_CMD', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE') or k.startswith('LC_')}
        row = dict(id=ident, request_schema=REQUEST_SCHEMA, agent=args.agent,
                   workspace=args.workspace, script=args.script, lane=lane, kind=args.kind,
                   argv=replay['argv'], env=env, return_route=route,
                   return_reservation=replay.get('return_reservation', ''),
                   submitted_wall=wall_now(), not_before=args.not_before,
                   runid=args.runid, label=args.label, logdir=args.logdir)
        _save('queue', row)
        return 'queued', ident, str(len(state['queue']) + 1)


def replay_argv(row):
    """Pin routing before a script's command separator, preserving command argv."""
    argv, out, index = list(row['argv']), [], 0
    while index < len(argv) and argv[index] != '--':
        if argv[index] in ('--return-route-json', '--return-reservation'):
            index += 2
            continue
        out.append(argv[index])
        index += 1
    if row.get('return_route'):
        from return_route import canonical
        out += ['--return-route-json', canonical(row['return_route']).decode(),
                '--return-reservation', row.get('return_reservation', '')]
    return out + argv[index:]


def _spawn(lease):
    row = lease['ticket_doc']
    env = dict(row['env'], DOGANY_HOST_LEASE=lease['id'], DOGANY_HOST_RUNID=lease.get('runid', ''))
    argv = replay_argv(row)
    directory = row.get('logdir') or root()
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, 'queue-' + row['id'] + '.log'), 'ab') as out:
        subprocess.Popen([row['script']] + argv, env=env, cwd=row['workspace'] or None,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True, close_fds=True)


def cmd_drain(args):
    machine = machine_read()
    grants = []
    with host_lock():
        state = load_state()
        _reap(state)
        # FIFO within each resource lane. Unrecognized historical requests
        # remain untouched for their owning adapter's explicit migration.
        held = set()
        for row in sorted(state['queue'].values(), key=lambda r: (r.get('submitted_wall', 0), r['id'])):
            if row.get('request_schema') != REQUEST_SCHEMA:
                continue
            lane = row['lane']
            if lane in held:
                continue
            held.add(lane)
            if row.get('not_before', 0) > wall_now():
                continue
            runtime, _, old_account = lane.partition('@')
            account = account_for(runtime, env=row.get('env'))
            if account != old_account:
                row['lane'] = runtime + '@' + account
                _save('queue', row)
                continue  # execution refresh must provide the new account cap
            if not _fits(state, lane, machine, row['kind'])[0]:
                continue
            lease = _grant(state, row)
            _unlink(_p('queue', row['id'] + '.json'))
            grants.append(lease)
    for lease in grants:
        try:
            _spawn(lease)
        except OSError:
            # The owning request stays durable when the executable disappears.
            with host_lock():
                _save('queue', lease['ticket_doc'])
                _unlink(_p('leases', lease['id'] + '.json'))
            raise
    return 'drained', '-', str(len(grants))


def cmd_bind(args):
    with host_lock():
        row = read_json(_p('leases', args.lease + '.json'))
        if not row:
            return 'missing', '-', 'lease not found'
        row.update(pid=args.pid, runid=args.runid, marker=args.marker)
        _save('leases', row)
    return 'bound', args.lease, ''


def cmd_release(args):
    with host_lock():
        _unlink(_p('leases', args.lease + '.json'))
    return 'released', args.lease, ''


def cmd_cancel(args):
    with host_lock():
        row = read_json(_p('queue', args.ticket + '.json'))
        if not row or row.get('agent') != args.agent or row.get('request_schema') != REQUEST_SCHEMA:
            return 'missing', args.ticket, 'request owner mismatch or absent'
        from return_route import release_reservation
        release_reservation(row['workspace'], row.get('return_reservation', ''))
        _unlink(_p('queue', args.ticket + '.json'))
    return 'cancelled', args.ticket, ''


def cmd_refresh(args):
    with host_lock():
        state = load_state()
        lane = _refresh(state, args)
    return 'refreshed', lane, ''


def r3_reserve(request, binding):
    from record_provider import configured
    provider = configured()
    reserve = getattr(provider, 'host_reserve', None)
    if reserve is None:
        return dict(state='unavailable', reason='configured-record-provider-required')
    return reserve(request, binding)


def main(argv=None):
    driver = os.environ.get('DOGANY_HOST_DRIVER')
    if driver:
        os.execv(driver, [driver] + (sys.argv[1:] if argv is None else argv))
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for verb in ('acquire', 'enqueue', 'refresh'):
        p = sub.add_parser(verb)
        p.add_argument('--runtime', choices=LOCAL_RUNTIMES, required=True)
        p.add_argument('--account', default='unknown')
        p.add_argument('--cap', type=int)
        p.add_argument('--cap-note', default='')
        if verb == 'refresh':
            continue
        p.add_argument('--agent', required=True)
        for key in ('workspace', 'script', 'display', 'label', 'model', 'runid'):
            p.add_argument('--' + key, default='')
        p.add_argument('--kind', choices=RUN_KINDS, default='build')
        if verb == 'acquire':
            p.add_argument('--pid', type=int, required=True)
            p.add_argument('--lease', default='')
            p.add_argument('--force', action='store_true')
        else:
            p.add_argument('--argv-file', required=True)
            p.add_argument('--logdir', default='')
            p.add_argument('--why', default='')
            p.add_argument('--not-before', type=float, default=0)
    for verb in ('bind', 'release'):
        p = sub.add_parser(verb)
        p.add_argument('--lease', required=True)
        if verb == 'bind':
            p.add_argument('--pid', type=int, required=True)
            p.add_argument('--runid', default='')
            p.add_argument('--marker', default='')
    p = sub.add_parser('cancel')
    p.add_argument('--agent', required=True)
    p.add_argument('--ticket', required=True)
    sub.add_parser('drain')
    p = sub.add_parser('list')
    p.add_argument('--json', action='store_true')
    sub.add_parser('machine')
    p = sub.add_parser('account')
    p.add_argument('--runtime', choices=LOCAL_RUNTIMES, required=True)
    p.add_argument('--api-dir', default='')
    p = sub.add_parser('reap-scratch')
    p.add_argument('--min-age-h', default='6')
    p.add_argument('--tmp-min-age-h', default='24')
    p.add_argument('--tmpdir', default='')
    p.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'account':
            print(account_for(args.runtime, args.api_dir))
        elif args.command == 'machine':
            with host_lock():
                state = load_state()
                _reap(state)
            print('%d\t%s' % machine_room(state, machine_read()))
        elif args.command == 'list':
            with host_lock():
                state = load_state()
                _reap(state)
            print(json.dumps(state, sort_keys=True))
        elif args.command == 'reap-scratch':
            return cmd_reap_scratch(args)
        else:
            verdict, ident, note = globals()['cmd_' + args.command](args)
            print('%s\t%s\t%s' % (verdict, ident, ' '.join(str(note).split())))
        return 0
    except (OSError, ValueError, Unavailable) as error:
        print('unavailable\t-\t' + str(error))
        return 3


if __name__ == '__main__':
    sys.exit(main())
