# DGN-1869: shared additive registry writer, extracted from update_seed.sh.
# Requires PACK_PY3. Optional fourth argument guards new crew membership;
# an existing row remains a no-write operation during a crew transaction.
# Uses the same conf.lock as seed and crew_leave; preserves other members.
_line_upsert() {
  local conf="$1" line="$2" uniq="$3" crew="${4:-}"
  "$PACK_PY3" - "$conf" "$line" "$uniq" "$crew" <<'PYEOF'
import fcntl, os, sys, tempfile, time, pathlib
conf = pathlib.Path(sys.argv[1])
line, uniq, crew = sys.argv[2:5]
lock_path = str(conf) + ".lock"
timeout = float(os.environ.get("DOGANY_CONF_LOCK_TIMEOUT", "30"))
conf.parent.mkdir(parents=True, exist_ok=True)
lock_fh = open(lock_path, "a")
deadline = time.monotonic() + timeout
while True:
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if time.monotonic() >= deadline:
            sys.stderr.write(
                "[seed] FATAL: could not acquire %s within %.0fs "
                "(concurrent writer stuck?) -- conf untouched\n" % (lock_path, timeout))
            sys.exit(1)
        time.sleep(0.05)
try:
    lines = conf.read_text(encoding="utf-8").splitlines() if conf.exists() else []
    if line in lines:
        print("[seed]   present, untouched: %s" % line)
        sys.exit(0)
    if uniq and any(ln.startswith(uniq) for ln in lines):
        old = next(ln for ln in lines if ln.startswith(uniq))
        print("[seed]   PRESERVED existing (additive contract, not overwritten): "
              "%s (seed wanted: %s)" % (old, line))
        sys.exit(0)
    if crew and ((conf.parent / ".lease").exists() or
                 (conf.parent / "crew.declared").exists()):
        sys.stderr.write("[crew-register] registry owned by a lease or declared crew: %s\n" % conf)
        sys.exit(1)
    lines.append(line)
    data = "\n".join(lines) + "\n"
    mode = conf.stat().st_mode & 0o7777 if conf.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".seed.tmp.", dir=str(conf.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, conf)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(str(conf.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    print("[seed]   appended: %s" % line)
finally:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()
PYEOF
}
