#!/usr/bin/env python3
"""anchor_crossref_lint -- anchor <-> child release cross-ref drift lint.

DGN-999 machinery (direction B: read-only lint, no auto-edit), landed live
on Metal and carried back to the canonical template by DGN-1013 item 5. The
anchor's release_tickets/release_bundle_* frontmatter is hand-maintained and
has leaked before (observed on Metal: DGN-960 redesign, DGN-1031 counter
blackout). A child ticket's own frontmatter `release: vX.Y` declaration is
the truth source; this lint derives every mismatch between that declaration
layer and the anchor's hand-kept lists and reports it LOUDLY. It never
writes: worklog is the owning session's sole-write surface, so an
auto-editing machine would introduce a second writer -- rejected by design.

Drift categories (exit 1 when any is non-empty):
  missing            child declares an active anchor version, anchor's
                     release_tickets lacks it.
  undeclared         anchor lists a ticket whose own frontmatter does NOT
                     declare that version (absent field / unassigned marker /
                     other version). Forces child declarations to converge
                     to the truth-source contract.
  phantom            release_tickets entry with no resolvable worklog file.
  bundle_stray       ticket in a release_bundle_* list but not in
                     release_tickets (counters diverge).
  bundle_unassigned  ticket in release_tickets but in no bundle while the
                     anchor declares release_bundles (owner must assign --
                     the lint NEVER guesses a bundle).
  invisible          active anchor whose frontmatter is unreadable by the
                     workbench consumer (status-footer._ticket_frontmatter's
                     _FRONTMATTER_SCAN_LINES cliff, DGN-1013 item D) or whose
                     release: value fails the consumer's strict version regex.

Warn categories (printed + counted, exit stays 0):
  unassigned_release ticket in a non-terminal status whose release: field
                     carries no version token (e.g. "(unassigned)" markers).
                     These sit OUTSIDE every anchor counter -- the exact
                     pre-correction DGN-1031 blackout shape -- and need a
                     human assignment decision (or a deliberate park).
  headroom           active anchor's closing fence within 2 lines of the
                     consumer's fence cliff (one more frontmatter key may
                     silently drop the anchor from the board).
  malformed          file whose frontmatter fence opens but never closes
                     (DGN-960 once shipped in that state).

Zero-vs-not-run discipline: the report ALWAYS ends with a single greppable
"SUMMARY scanned=<n> anchors_active=<m> drift=<k> warn=<l>" line; a missing
SUMMARY line means the scan did not run. Structural failure (worklog dir
unreadable, zero tickets, unreadable active anchor) exits 2 with a loud
"SCAN-FAIL:" line -- never a quiet empty report.

Read-only by construction: the module only ever opens worklog files for
reading. Exit codes: 0 clean, 1 drift, 2 scan failure.

Portability (DGN-1013 carryback): the Metal original hardcoded the "DGN-"
ticket prefix throughout (ticket-file filter + id regex), so it would
silently under-scan or mis-scan on any instance using a different prefix --
the same class of bug DGN-1052 fixed in ticket-hygiene.sh. This version
resolves the prefix the same way (env TICKET_PREFIX -> config/agent.conf
TICKET_PREFIX= -> a generic "<letters>-<digits>" id shape when unset) and
the ticket-file glob via TICKET_FILE_GLOB (same env/conf/fallback order as
ticket-status-enum-lint.py and ticket-hygiene.sh).
"""

import fnmatch
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from conf_reader import conf_get
except Exception:
    def conf_get(key, conf_path=None):
        return ""


def _root_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _conf_get(key):
    path = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(
        _root_dir(), "config", "agent.conf")
    return conf_get(key, path)


def _ticket_prefix():
    return os.environ.get("TICKET_PREFIX") or _conf_get("TICKET_PREFIX")


def _ticket_file_glob():
    """DGN-1052 precedent: env -> conf -> "<prefix>-*.md" -> "*.md"."""
    glob = os.environ.get("TICKET_FILE_GLOB") or _conf_get("TICKET_FILE_GLOB")
    if glob:
        return glob
    prefix = _ticket_prefix()
    return "%s-*.md" % prefix if prefix else "*.md"


def _tid_re():
    """Ticket-id token pattern. A configured TICKET_PREFIX narrows the match
    to "<PREFIX>-<digits>"; unset falls back to a generic "<letters>-<digits>"
    shape (matches DGN-999, WRG-12, ... without assuming a specific one)."""
    prefix = _ticket_prefix()
    if prefix:
        return re.compile(r"%s-\d+" % re.escape(prefix), re.IGNORECASE)
    return re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+", re.IGNORECASE)


# Keep every parsing rule aligned with the consumer (routines/status-footer.py
# _ticket_frontmatter / _collect_release_pipeline) so the lint judges the
# anchor exactly as the workbench counter will read it.
_FM_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*\S)?\s*$")
# Consumer's strict anchor version form (full match): "v2.0" / "1.39.3".
_VERSION_STRICT_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")
# Child declarations carry measured real-world tails ("v1.22.2 (patch) -- ..."):
# accept a LEADING version token for children.
_VERSION_LEAD_RE = re.compile(r"^v?(\d+(?:\.\d+)*)\b")
_DONE_STATUSES = ("done",)
_EXCLUDED_STATUSES = ("dismissed", "aborted", "cancelled", "canceled")
_ACTIVE_ANCHOR_STATES = ("in-progress", "queued")
# Full-fidelity fence bound for THIS lint (not the consumer's bound).
_FM_HARD_BOUND = 200
# status-footer._ticket_frontmatter's _FRONTMATTER_SCAN_LINES cliff
# (DGN-1013 item D): opening fence (line 1) + this many more lines. A
# closing fence past that line -> consumer returns {} (anchor invisible).
_CONSUMER_FENCE_CLIFF = 1 + 200
_HEADROOM_WARN_MARGIN = 2


class ScanError(RuntimeError):
    """Structural scan failure -- caller must exit 2, never render 'clean'."""


def parse_frontmatter(path):
    """Full frontmatter read (no consumer line-cap).

    Returns (fm_dict, close_lineno) on success; (None, "no_frontmatter")
    when line 1 is not a fence; (None, "unclosed_fence") when the fence
    never closes within _FM_HARD_BOUND lines. IO errors raise (caller
    decides loudness).
    """
    fm = {}
    with open(path, "r", errors="replace") as fh:
        first = fh.readline()
        if first.strip() != "---":
            return None, "no_frontmatter"
        lineno = 1
        while lineno < _FM_HARD_BOUND:
            line = fh.readline()
            if not line:
                return None, "unclosed_fence"
            lineno += 1
            stripped = line.strip()
            if stripped == "---":
                return fm, lineno
            m = _FM_KV_RE.match(stripped)
            if m:
                fm[m.group(1)] = (m.group(2) or "").strip()
    return None, "unclosed_fence"


def _norm_tid(raw, tid_re):
    """First ticket-id token of a string, uppercased; None when absent."""
    m = tid_re.search(raw or "")
    return m.group(0).upper() if m else None


def _child_version(release_raw):
    """Normalized 'vX.Y' from a child release: value (leading token), else
    None (absent field or unassigned-marker prose)."""
    m = _VERSION_LEAD_RE.match((release_raw or "").strip())
    return ("v" + m.group(1)) if m else None


def _resolve_main_file(tid, prefix_index, fm_cache, tid_re):
    """Pick the file that speaks for a ticket id -- same disambiguation as
    status-footer._resolve_ticket_status: single candidate wins; else the
    sorted-first file whose id: matches; else a lone id-less file.
    Returns filename or None."""
    cands = prefix_index.get(tid) or []
    if len(cands) == 1:
        return cands[0]
    idless = []
    for c in sorted(cands):
        fm = fm_cache.get(c)
        cid = _norm_tid((fm or {}).get("id"), tid_re) if fm else None
        if cid == tid:
            return c
        if fm is not None and not cid:
            idless.append(c)
    if len(idless) == 1:
        return idless[0]
    return None


def scan(worklog_dir, ticket_glob=None, tid_re=None):
    """Scan the worklog and return the drift report dict.

    Raises ScanError on structural failure (see module docstring)."""
    ticket_glob = ticket_glob if ticket_glob is not None else _ticket_file_glob()
    tid_re = tid_re if tid_re is not None else _tid_re()
    try:
        names = sorted(os.listdir(worklog_dir))
    except OSError as e:
        raise ScanError("worklog dir unreadable: %s (%s)" % (worklog_dir, e))

    ticket_files = [n for n in names
                    if not n.startswith("_") and n.endswith(".md")
                    and fnmatch.fnmatch(n, ticket_glob)]
    if not ticket_files:
        raise ScanError("zero ticket files (glob=%s) in %s"
                        % (ticket_glob, worklog_dir))

    fm_cache = {}       # filename -> fm dict (or None on parse failure)
    fence_close = {}    # filename -> closing-fence line number
    malformed = []      # unclosed fence
    no_frontmatter = 0
    prefix_index = {}   # ticket-id -> [filenames]

    for name in ticket_files:
        tid = _norm_tid(name, tid_re)
        if tid:
            prefix_index.setdefault(tid, []).append(name)
        try:
            fm, extra = parse_frontmatter(os.path.join(worklog_dir, name))
        except OSError:
            fm, extra = None, "unreadable"
        if fm is None:
            fm_cache[name] = None
            if extra == "no_frontmatter":
                no_frontmatter += 1
            else:
                malformed.append("%s (%s)" % (name, extra))
        else:
            fm_cache[name] = fm
            fence_close[name] = extra

    # --- resolve one speaking file per ticket id -----------------------------
    main_file = {}      # tid -> filename or None
    for tid in prefix_index:
        main_file[tid] = _resolve_main_file(tid, prefix_index, fm_cache, tid_re)

    def _fm_of(tid):
        f = main_file.get(tid)
        return fm_cache.get(f) if f else None

    # --- collect anchors (consumer predicate: filename contains "anchor") ---
    anchors = []
    anchor_tids = set()
    for name in ticket_files:
        if "anchor" not in name.lower():
            continue
        fm = fm_cache.get(name)
        if fm is None:
            if name in [m.split(" ")[0] for m in malformed]:
                raise ScanError("anchor frontmatter unreadable: %s" % name)
            continue
        state = (fm.get("release_state") or "").strip().lower()
        state = state.replace("_", "-")
        if state not in _ACTIVE_ANCHOR_STATES:
            continue
        tid = _norm_tid(fm.get("id"), tid_re) or _norm_tid(name, tid_re)
        ver_raw = (fm.get("release") or "").strip()
        strict = _VERSION_STRICT_RE.match(ver_raw)
        anchors.append({
            "tid": tid,
            "name": name,
            "state": state,
            "version": ("v" + strict.group(1)) if strict else None,
            "version_raw": ver_raw,
            "fence_close": fence_close.get(name, 0),
            "fm": fm,
        })
        if tid:
            anchor_tids.add(tid)

    # Any REAL anchor tid (active or shipped) is excluded from the
    # child-declaration axis -- an anchor legitimately declares its own
    # release without listing itself. "Real anchor" = anchor-named file
    # that carries a release_state key; a mere name hit stays a normal child.
    for name in ticket_files:
        if "anchor" not in name.lower():
            continue
        fm = fm_cache.get(name)
        if fm is None or "release_state" not in fm:
            continue
        t = _norm_tid(fm.get("id"), tid_re) or _norm_tid(name, tid_re)
        if t:
            anchor_tids.add(t)

    # --- child declaration index: tid -> declared version -------------------
    declared = {}       # tid -> "vX.Y"
    unassigned_release = []   # (tid, status, raw) -- active status, no token
    other_versions = {}       # version -> count (no active anchor)
    active_versions = set(a["version"] for a in anchors if a["version"])
    for tid in sorted(prefix_index):
        fm = _fm_of(tid)
        if fm is None:
            continue
        raw = fm.get("release")
        if raw is None:
            continue
        status = (fm.get("status") or "").strip().lower()
        ver = _child_version(raw)
        if ver is None:
            if (status not in _DONE_STATUSES
                    and status not in _EXCLUDED_STATUSES):
                unassigned_release.append((tid, status or "?", raw))
            continue
        declared[tid] = ver
        if ver not in active_versions and tid not in anchor_tids:
            other_versions[ver] = other_versions.get(ver, 0) + 1

    # --- per-version anchor list union --------------------------------------
    by_version = {}     # version -> {"anchors": [...], "tickets": set,
                        #             "bundles": {slug: set}}
    drift_total = 0
    warn_total = 0
    orphan_invisible = []   # active anchors the consumer cannot even version
    for a in anchors:
        if not a["version"]:
            a["invisible"] = ("release '%s' fails the consumer version "
                              "regex -- anchor invisible to the workbench"
                              % a["version_raw"])
            orphan_invisible.append(a)
            drift_total += 1
            continue
        a["invisible"] = None
        if a["fence_close"] > _CONSUMER_FENCE_CLIFF:
            a["invisible"] = (
                "frontmatter closes at line %d > consumer cliff %d -- "
                "workbench silently drops this anchor"
                % (a["fence_close"], _CONSUMER_FENCE_CLIFF))
            drift_total += 1
        a["headroom_warn"] = (
            a["invisible"] is None
            and a["fence_close"] > _CONSUMER_FENCE_CLIFF
            - _HEADROOM_WARN_MARGIN)
        if a["headroom_warn"]:
            warn_total += 1
        slot = by_version.setdefault(
            a["version"], {"anchors": [], "tickets": set(), "bundles": {}})
        slot["anchors"].append(a)
        fm = a["fm"]
        for t in tid_re.findall(fm.get("release_tickets") or ""):
            slot["tickets"].add(t.upper())
        for key, val in fm.items():
            if key.startswith("release_bundle_"):
                slug = key[len("release_bundle_"):]
                slot["bundles"].setdefault(slug, set()).update(
                    t.upper() for t in tid_re.findall(val or ""))

    # --- drift computation ---------------------------------------------------
    version_reports = []
    for ver in sorted(by_version):
        slot = by_version[ver]
        tickets = slot["tickets"]
        bundles = slot["bundles"]
        bundled = set()
        for members in bundles.values():
            bundled |= members

        missing = []
        for tid, dver in sorted(declared.items()):
            if dver != ver or tid in anchor_tids or tid in tickets:
                continue
            fm = _fm_of(tid) or {}
            status = (fm.get("status") or "?").strip().lower()
            if status in _EXCLUDED_STATUSES:
                continue
            missing.append((tid, status))

        undeclared = []
        phantom = []
        bundle_unassigned = []
        for tid in sorted(tickets):
            fm = _fm_of(tid)
            if fm is None and main_file.get(tid) is None:
                phantom.append(tid)
                continue
            status = ((fm or {}).get("status") or "?").strip().lower()
            if status in _EXCLUDED_STATUSES:
                continue
            if declared.get(tid) != ver:
                undeclared.append(
                    (tid, status, ((fm or {}).get("release") or "(absent)")))
            if bundles and tid not in bundled:
                bundle_unassigned.append((tid, status))

        bundle_stray = sorted(bundled - tickets)

        n_drift = (len(missing) + len(undeclared) + len(phantom)
                   + len(bundle_stray) + len(bundle_unassigned))
        drift_total += n_drift
        version_reports.append({
            "version": ver,
            "anchors": slot["anchors"],
            "tickets": len(tickets),
            "missing": missing,
            "undeclared": undeclared,
            "phantom": phantom,
            "bundle_stray": bundle_stray,
            "bundle_unassigned": bundle_unassigned,
            "drift": n_drift,
        })

    warn_total += len(unassigned_release) + len(malformed)

    return {
        "worklog_dir": worklog_dir,
        "files": len(ticket_files),
        "ids": len(prefix_index),
        "no_frontmatter": no_frontmatter,
        "malformed": malformed,
        "anchors_active": len(anchors),
        "anchors_invisible_orphan": orphan_invisible,
        "versions": version_reports,
        "unassigned_release": unassigned_release,
        "other_versions": other_versions,
        "drift": drift_total,
        "warn": warn_total,
    }


def dashboard_line(report):
    """One compact Korean line for the workbench (status-footer consumes
    this). Empty string when there is no drift -- warn-only states stay off
    the board (full detail lives in the CLI report)."""
    if report["drift"] == 0:
        return ""
    parts = []
    for vr in report["versions"]:
        cats = [("누락", len(vr["missing"])),
                ("미선언", len(vr["undeclared"])),
                ("유령", len(vr["phantom"])),
                ("번들이탈", len(vr["bundle_stray"])),
                ("번들미배정", len(vr["bundle_unassigned"]))]
        toks = ["%s %d" % (label, n) for label, n in cats if n]
        inv = sum(1 for a in vr["anchors"] if a.get("invisible"))
        if inv:
            toks.append("앵커비가시 %d" % inv)
        if toks:
            parts.append("%s %s" % (vr["version"], " · ".join(toks)))
    orphan_inv = report.get("anchors_invisible_orphan") or []
    if orphan_inv:
        parts.append("앵커비가시 %d" % len(orphan_inv))
    return "⚠ 앵커정합 " + " / ".join(parts) if parts else ""


def format_report(report):
    out = []
    out.append("anchor-crossref-lint: %s" % report["worklog_dir"])
    out.append("scanned: files=%d ids=%d frontmatter_ok=%d "
               "no_frontmatter=%d malformed=%d"
               % (report["files"], report["ids"],
                  report["files"] - report["no_frontmatter"]
                  - len(report["malformed"]),
                  report["no_frontmatter"], len(report["malformed"])))
    out.append("active anchors: %d" % report["anchors_active"])
    for a in report.get("anchors_invisible_orphan") or []:
        out.append("DRIFT invisible: %s (%s): %s"
                   % (a["tid"], a["name"], a["invisible"]))
    for vr in report["versions"]:
        heads = ", ".join("%s (%s, %s)" % (a["tid"], a["name"], a["state"])
                          for a in vr["anchors"])
        out.append("[%s] anchor=%s release_tickets=%d drift=%d"
                   % (vr["version"], heads, vr["tickets"], vr["drift"]))
        for a in vr["anchors"]:
            if a.get("invisible"):
                out.append("  DRIFT invisible: %s" % a["invisible"])
            elif a.get("headroom_warn"):
                out.append("  WARN headroom: frontmatter closes at line %d "
                           "(consumer cliff at %d -- %d line(s) left)"
                           % (a["fence_close"], _CONSUMER_FENCE_CLIFF,
                              _CONSUMER_FENCE_CLIFF - a["fence_close"]))
        def _cat(label, rows, fmt):
            out.append("  DRIFT %s: %d" % (label, len(rows)))
            for r in rows:
                out.append("    - %s" % fmt(r))
        _cat("missing (child declares %s, anchor list lacks it)"
             % vr["version"], vr["missing"],
             lambda r: "%s (status=%s)" % r)
        _cat("undeclared (anchor lists it, child release: != %s)"
             % vr["version"], vr["undeclared"],
             lambda r: "%s (status=%s, release=%s)" % r)
        _cat("phantom (listed, no ticket file)", vr["phantom"],
             lambda r: r)
        _cat("bundle_stray (in a bundle, not in release_tickets)",
             vr["bundle_stray"], lambda r: r)
        _cat("bundle_unassigned (in release_tickets, no bundle -- "
             "owner must assign)", vr["bundle_unassigned"],
             lambda r: "%s (status=%s)" % r)
    out.append("WARN unassigned_release (release field carries no version, "
               "status non-terminal -- invisible to every anchor counter): %d"
               % len(report["unassigned_release"]))
    for tid, status, raw in report["unassigned_release"]:
        out.append("  - %s (status=%s, release=%s)" % (tid, status, raw))
    if report["malformed"]:
        out.append("WARN malformed frontmatter (fence never closes): %d"
                   % len(report["malformed"]))
        for m in report["malformed"]:
            out.append("  - %s" % m)
    if report["other_versions"]:
        out.append("declared versions without an active anchor "
                   "(stats only): %s"
                   % ", ".join("%s=%d" % kv for kv in
                               sorted(report["other_versions"].items())))
    out.append("SUMMARY scanned=%d anchors_active=%d drift=%d warn=%d"
               % (report["files"], report["anchors_active"],
                  report["drift"], report["warn"]))
    return "\n".join(out)


def main(argv):
    worklog = None
    for arg in argv[1:]:
        if arg in ("-h", "--help"):
            print(__doc__)
            return 0
        worklog = arg
    if worklog is None:
        worklog = os.environ.get("DOGANY_WORKLOG_DIR") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "worklog")
    try:
        report = scan(worklog)
    except ScanError as e:
        print("SCAN-FAIL: %s" % e)
        return 2
    print(format_report(report))
    return 1 if report["drift"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
