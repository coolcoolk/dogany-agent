"""contract_routing -- write-side slot classification and routing
(DGN-2028 / DGN-2010 slice 4, dec-441 item 2; merged design section 6).

Nightly consolidation classifies every kept item into a base slot and routes
a kit-contract fact to the kit that declares a home for it, instead of storing
everything as memory. Only cmd_consolidate calls this module, and only while
CONSOLIDATE_ROUTES is on; with the flag off it is never imported.

One item representation (section 6): stable item id and intent id, source
event reference and source span, slot / target / time / about, provenance
(the DGN-1384 subject), classification state and evidence references.

Routing order (section 6):
  1. validate attribution and source evidence;
  2. resolve slot and target (structured KEEP classification);
  3. resolve the installed home (resolve_home: 0 kit bindings = base,
     1 = that kit, 2+ = agent-owned contract_overlap hold);
  4. reconcile through the provider (read-only `lookup`);
  5. persist intent / outbox (persist) -- the caller does this before the
     source watermark moves, and never on a dry run;
  6. execute only an authorized held door -- NOT in slice 4: a write binding
     stays `door_pending` (provider write adapters are slice 5);
  7. verify receipt -- slice 5, same reason;
  8. the caller advances the watermark once every item is durable.

Provider protocol (section 3): `lookup` returns absent | equivalent |
compatible | conflict | unknown; `changes` a bounded delta plus a resumable
cursor; `evidence` local snapshots. All three are read operations. The adapter
is an installed file inside the instance (realpath containment), invoked as an
argv list with shell=False and a JSON request on stdin; its operation name is
never taken from memory text. Any failure is unavailable / unknown, never
"no record".

Slots `preference` / durable owner `state` are reserved for DGN-2120 and land
in base memory slot-tagged. Runtime state is never consolidated; its outbox
receipt expires after one day and requires current-runtime verification.
A `why` item is a Why clue: base memory with its
source reference, never a Why node (no nightly Why generation, AMEND-2 1).

Stdlib only. ASCII only.
"""
import datetime
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import unicodedata

FLAG_KEY = "CONSOLIDATE_ROUTES"
PENDING_MD = "routed-pending.md"
TIMES = ("now", "past", "prediction")
ABOUT = ("owner", "person", "agent", "host", "product")
CLASS_KEYS = ("slot", "target", "time", "about")
READ_OPS = ("lookup", "changes", "evidence")
LOOKUP_RESULTS = ("absent", "equivalent", "compatible", "conflict", "unknown")
RECON_STATES = ("open", "asked", "deferred", "resolved")
RECON_KINDS = ("memory", "kit", "decision")
PROVIDER_TIMEOUT_SECS = 10.0
MAX_REFS = 16
MAX_EVIDENCE = 8
MAX_CHANGES = 200
MAX_REPLY_BYTES = 256 * 1024
SUBJECT_OWNER = "owner-quoted"
SUBJECT_UNVERIFIED = "unverified"
_TOKEN_RE = re.compile(r"^([a-z]+)=([A-Za-z0-9_.:-]+)$")

_BC_CACHE = {}


def base_contract(root):
    """The one base_contract validator, loaded from the instance's
    routines/lib (never a second copy of its shape rules)."""
    path = os.path.join(root, "routines", "lib", "base_contract.py")
    mod = _BC_CACHE.get(path)
    if mod is None:
        spec = importlib.util.spec_from_file_location("_routes_base_contract", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BC_CACHE[path] = mod
    return mod


def installed_contracts(root):
    """[(pack_id, contract)] for every installed record that validates."""
    try:
        return base_contract(root).load_installed(root)
    except (OSError, ImportError, AttributeError, SyntaxError):
        return []


def slot_names(root):
    bc = base_contract(root)
    return tuple(bc.SLOTS) + tuple(bc.PROPOSAL_SLOTS) + tuple(bc.RESERVED_SLOTS)


def declared_targets(contracts):
    """[(target_id, pack_id)] in pack order, one row per declaring kit."""
    out = []
    for pack, c in contracts:
        for t in c.get("targets", []):
            out.append((t["id"], pack))
    return out


# ---------------------------------------------------------------------------
# prompts (appended only when the flag is on)
# ---------------------------------------------------------------------------

EXTRACT_ADDENDUM = (
    "Routing split (CONSOLIDATE_ROUTES): when one statement mixes an observed value with its "
    "cause, or with a decision, emit one item per part (observation / cause / decision), each "
    "with the same subject mark and the same quote span. Keep a stated cause as its own item "
    "even when it is not confirmed (it is stored as an explanation, never as a fact). Keep a "
    "stated reason or purpose of __USER_LABEL__ as its own item.\n"
    "\n"
)


def filter_addendum(slots, targets):
    """The structured-KEEP instruction for the second-stage batch. It rides
    the existing batch call (no extra model call)."""
    tl = ", ".join(sorted({t for t, _ in targets})) or "(none installed)"
    return (
        "Routing mode (CONSOLIDATE_ROUTES) -- overrides the KEEP line format above.\n"
        "Every KEEP line carries its classification, exactly:\n"
        "'<n> KEEP slot=<slot> target=<target or -> time=<now|past|prediction> "
        "about=<owner|person|agent|host|product>'\n"
        "slot (what the information is for): " + ", ".join(slots) + ".\n"
        "- explanation = a stated cause ('X because Y'); an unconfirmed cause may be KEPT as "
        "slot=explanation, never as a fact.\n"
        "- why = a stated reason or purpose of __USER_LABEL__ behind a choice.\n"
        "- state = current status; runtime status about host/agent/product is ephemeral "
        "and will not become memory. Stable product facts use context instead.\n"
        "- time=prediction = an expectation about the future; it may be KEPT although unconfirmed.\n"
        "target (what it concerns): one of [" + tl + "], or - when none fits.\n"
        "about (whose state it describes): owner = __USER_LABEL__, person = another person, "
        "agent = the assistant itself, host = the machine, product = a product and its users.\n"
        "DROP lines keep the format '<n> DROP <reason>'.\n"
        "\n"
    )


def parse_classification(tail, slots, target_ids):
    """Validate the KEEP tail. Returns (cls, None) or (None, why).
    A malformed or partial tail is never guessed into a slot."""
    if not isinstance(tail, str) or not tail.strip():
        return None, "malformed:empty"
    kv = {}
    for tok in tail.split():
        m = _TOKEN_RE.match(tok)
        if not m:
            return None, "malformed:token"
        k, v = m.group(1), m.group(2)
        if k in kv:
            return None, "malformed:duplicate-" + k
        kv[k] = v
    if set(kv) != set(CLASS_KEYS):
        return None, "malformed:keys"
    if kv["slot"] not in slots:
        return None, "malformed:slot"
    if kv["time"] not in TIMES:
        return None, "malformed:time"
    if kv["about"] not in ABOUT:
        return None, "malformed:about"
    target = None if kv["target"] == "-" else kv["target"]
    if target is not None and target not in target_ids:
        return None, "malformed:target"
    return {"slot": kv["slot"], "target": target, "time": kv["time"],
            "about": kv["about"]}, None


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def norm_text(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip().lower()


def _h(*parts):
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]


def owner_event_id(owner_text, ts_iso, ordinal):
    """The original source event shared across crew observations: the owner
    message's words, its UTC date and its ordinal among identical messages
    of that date. Two real identical payments stay two events."""
    return "ev-" + _h("owner", norm_text(owner_text), (ts_iso or "")[:10], str(ordinal))


def local_event_id(instance, text):
    """An item with no owner source (agent reading, unverified) is an
    independent observation of one instance."""
    return "ev-" + _h("local", instance or "", norm_text(text))


def item_ids(event_id, text, slot, target):
    """(item_id, intent_id). The intent id carries no destination, so a
    destination change migrates the same intent."""
    t = norm_text(text)
    return ("itm-" + _h(event_id, t),
            "int-" + _h(event_id, slot or "", target or "", t))


# ---------------------------------------------------------------------------
# home resolution
# ---------------------------------------------------------------------------

def binding_version(contract, binding):
    return "bv-" + _h(str(contract.get("version")),
                      json.dumps(binding, sort_keys=True, ensure_ascii=True))


def resolve_home(slot, target_id, contracts):
    """0 matching kit bindings = base, 1 = that binding, 2+ = an agent-owned
    contract_overlap hold (no alphabetical winner, no owner question).
    A `none` binding withdraws that kit's claim before counting."""
    if target_id is None:
        return {"home": "base", "reason": "no_target"}
    matches = []
    for pack, c in contracts:
        for b in c.get("bindings", []):
            if b.get("slot") == slot and b.get("target") == target_id \
                    and b.get("mode") != "none":
                matches.append((pack, c, b))
    if not matches:
        return {"home": "base", "reason": "no_kit_binding"}
    if len(matches) > 1:
        return {"home": "hold", "reason": "contract_overlap",
                "packs": sorted(m[0] for m in matches)}
    pack, c, b = matches[0]
    return {"home": "kit", "reason": "binding", "pack": pack, "binding": b,
            "provider": c["providers"][b["provider"]],
            "binding_version": binding_version(c, b)}


# ---------------------------------------------------------------------------
# provider protocol client (read-only)
# ---------------------------------------------------------------------------

def _str_list(v, cap):
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, str) and x][:cap]


class ProviderClient:
    """Read-only provider calls. One failure marks that provider unavailable
    for the rest of the run (bounded: a dead adapter costs one timeout)."""

    def __init__(self, root, contracts, timeout=PROVIDER_TIMEOUT_SECS, runner=None):
        self.root = os.path.realpath(root)
        self.contracts = dict(contracts)
        self.timeout = timeout
        self.runner = runner or subprocess.run
        self.down = {}
        self.calls = 0

    def _adapter(self, pack, name):
        c = self.contracts.get(pack)
        p = (c or {}).get("providers", {}).get(name)
        if not p:
            return None, None, "undeclared_provider"
        full = os.path.realpath(os.path.join(self.root, p["adapter"]))
        if not full.startswith(self.root + os.sep) or not os.path.isfile(full):
            return None, None, "adapter_missing"
        return p, full, None

    def call(self, pack, name, op, request):
        """(True, reply dict) or (False, reason). `op` is one of the three
        read operations and must be declared by the provider."""
        key = (pack, name)
        if key in self.down:
            return False, self.down[key]
        if op not in READ_OPS:
            return False, "not_a_read_operation"
        p, full, err = self._adapter(pack, name)
        if err:
            self.down[key] = err
            return False, err
        if op not in p["operations"]:
            return False, "operation_not_declared"
        argv = ([sys.executable, full, op] if full.endswith(".py") else [full, op])
        body = dict(request)
        body.update({"op": op, "protocol": p["protocol"]})
        self.calls += 1
        try:
            proc = self.runner(argv, input=json.dumps(body, ensure_ascii=False),
                               capture_output=True, text=True, timeout=self.timeout,
                               shell=False, cwd=self.root)
        except (OSError, subprocess.SubprocessError) as e:
            self.down[key] = "unavailable:%s" % type(e).__name__
            return False, self.down[key]
        if proc.returncode != 0:
            self.down[key] = "unavailable:rc=%s" % proc.returncode
            return False, self.down[key]
        out = proc.stdout or ""
        if len(out.encode("utf-8", "replace")) > MAX_REPLY_BYTES:
            return False, "reply_too_large"
        try:
            reply = json.loads(out)
        except ValueError:
            return False, "reply_not_json"
        if not isinstance(reply, dict) or reply.get("protocol") != p["protocol"]:
            return False, "reply_protocol_mismatch"
        return True, reply

    def lookup(self, pack, name, request):
        ok, r = self.call(pack, name, "lookup", request)
        if not ok:
            return {"result": "unknown", "why": r, "refs": [], "versions": {},
                    "subject": None, "evidence": []}
        result = r.get("result")
        if result not in LOOKUP_RESULTS:
            return {"result": "unknown", "why": "bad_result", "refs": [], "versions": {},
                    "subject": None, "evidence": []}
        versions = r.get("versions") if isinstance(r.get("versions"), dict) else {}
        versions = {str(k): str(v) for k, v in list(versions.items())[:MAX_REFS]}
        subject = r.get("subject") if isinstance(r.get("subject"), str) else None
        return {"result": result, "why": None, "refs": _str_list(r.get("refs"), MAX_REFS),
                "versions": versions, "subject": subject,
                "evidence": _str_list(r.get("evidence"), MAX_EVIDENCE)}

    def changes(self, pack, name, cursor=None, limit=50):
        """Bounded delta + resumable cursor; on failure the caller keeps its
        old cursor (ok False)."""
        limit = max(1, min(int(limit), MAX_CHANGES))
        ok, r = self.call(pack, name, "changes", {"cursor": cursor, "limit": limit})
        if not ok:
            return {"ok": False, "why": r, "changes": [], "cursor": cursor}
        ch = r.get("changes")
        cur = r.get("cursor")
        if not isinstance(ch, list) or not isinstance(cur, str) or not cur:
            return {"ok": False, "why": "bad_changes_reply", "changes": [], "cursor": cursor}
        return {"ok": True, "why": None, "changes": ch[:limit], "cursor": cur,
                "truncated": len(ch) > limit}

    def evidence(self, pack, name, target, limit=MAX_EVIDENCE):
        limit = max(1, min(int(limit), MAX_EVIDENCE))
        ok, r = self.call(pack, name, "evidence", {"target": target, "limit": limit})
        if not ok:
            return {"ok": False, "why": r, "snapshots": []}
        snaps = r.get("snapshots")
        if not isinstance(snaps, list):
            return {"ok": False, "why": "bad_evidence_reply", "snapshots": []}
        return {"ok": True, "why": None,
                "snapshots": [s for s in snaps if isinstance(s, dict)][:limit]}


# ---------------------------------------------------------------------------
# planning: pure except read-only provider lookups (safe before a dry run)
# ---------------------------------------------------------------------------

def plan_item(item, contracts, client):
    """item: {text, subject, quote, event_id, cls, cls_error}. Returns the
    plan row. `lands` is where the text becomes retrievable: base (memory,
    deduped as today), pending (routed-pending.md, unresolved evidence) or
    referenced (an authoritative kit record covers it; no memory text), or
    none (an expiring runtime receipt, never a retrievable memory)."""
    cls = item.get("cls")
    slot = cls["slot"] if cls else None
    target = cls["target"] if cls else None
    item_id, intent_id = item_ids(item["event_id"], item["text"], slot, target)
    p = {"item_id": item_id, "intent_id": intent_id, "event_id": item["event_id"],
         "text": item["text"], "subject": item.get("subject"), "quote": item.get("quote"),
         "slot": slot, "target": target,
         "time": cls["time"] if cls else None, "about": cls["about"] if cls else None,
         "destination": "base", "state": "memory", "reason": None, "lands": "base",
         "binding": None, "binding_version": None, "refs": [], "versions": {},
         "evidence": [], "recon": None}

    def hold(reason, dest="hold"):
        p.update(destination=dest, state="hold", reason=reason, lands="pending")
        return p

    # 1-2. attribution / source evidence and classification
    if not cls:
        return hold(item.get("cls_error") or "unclassified")
    if slot == "state" and (p["about"] in ("host", "agent", "product")
                            or target == "runtime"):
        expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        p.update(destination="runtime", state="no_consolidate", lands="none",
                 reason="transient_runtime", expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 verification="verify_current_runtime_before_use")
        return p
    if slot in ("preference", "state"):
        p["reason"] = "reserved_dgn2120"
        return p
    if slot == "why":
        p["reason"] = "why_clue"
        return p
    # 3. installed home
    home = resolve_home(slot, target, contracts)
    if home["home"] == "base":
        p["reason"] = home["reason"]
        return p
    if home["home"] == "hold":
        p["evidence"] = ["packs:" + ",".join(home["packs"])]
        return hold("contract_overlap")
    b = home["binding"]
    p.update(destination="kit:" + home["pack"], binding=b,
             binding_version=home["binding_version"])
    if item.get("subject") == SUBJECT_UNVERIFIED:
        return hold("unverified_attribution", p["destination"])
    if slot == "decision" and item.get("subject") != SUBJECT_OWNER:
        return hold("decision_not_owner_quoted", p["destination"])
    # 4. reconcile through the provider (read-only)
    lk = client.lookup(home["pack"], b["provider"], {
        "slot": slot, "target": target, "text": item["text"], "quote": item.get("quote"),
        "subject": item.get("subject"), "time": p["time"], "about": p["about"],
        "event_id": item["event_id"], "intent_id": intent_id,
        "delivery_key": delivery_key(intent_id, p["destination"], p["binding_version"])})
    p.update(refs=lk["refs"], versions=lk["versions"], evidence=lk["evidence"])
    res = lk["result"]
    if res == "unknown":
        return hold("provider_" + (lk["why"] or "unknown"), p["destination"])
    if res == "equivalent":
        p.update(state="referenced", reason="equivalent", lands="referenced")
        return p
    if res == "conflict":
        p.update(state="conflict", reason="conflict", lands="pending",
                 recon={"kind": "decision" if slot == "decision" else "kit",
                        "subject_ref": lk["subject"] or ("intent:" + intent_id),
                        "target_id": target, "old_refs": lk["refs"],
                        "new_refs": [intent_id],
                        "evidence": lk["evidence"] + (
                            ["quote:" + item["quote"]] if item.get("quote") else []),
                        "version": lk["versions"]})
        return p
    # absent / compatible: the kit has no authoritative record of this item
    if b["mode"] == "write":
        p.update(state="door_pending", reason=res, lands="pending")
    elif res == "absent" and b.get("on_missing", "handoff") == "hold":
        p.update(state="hold", reason="absent_hold", lands="pending")
    else:
        p.update(state="handoff", reason=res, lands="pending")
    return p


def delivery_key(intent_id, destination, bversion):
    return "dk-" + _h(intent_id, destination or "", bversion or "")


def plan_items(items, contracts, client):
    return [plan_item(it, contracts, client) for it in items]


def route_tag(plan):
    """The slot/target/provenance suffix carried inside the stored tag
    group (the '(date, ...)' tail readers parse stays one group)."""
    parts = []
    if plan.get("slot"):
        parts.append("slot=" + plan["slot"])
    if plan.get("target"):
        parts.append("target=" + plan["target"])
    if plan.get("time"):
        parts.append("time=" + plan["time"])
    if plan.get("about"):
        parts.append("about=" + plan["about"])
    if plan["lands"] == "pending":
        parts.append("route=%s:%s" % (plan["state"], plan["reason"]))
        parts.append("intent=" + plan["intent_id"])
    parts.append("ev=" + plan["event_id"])
    return ", ".join(parts)


def tag_line(subject_tagged, plan):
    """Insert the route suffix before the final ')' of a _subject_tag line."""
    assert subject_tagged.endswith(")")
    return subject_tagged[:-1] + ", " + route_tag(plan) + ")"


# ---------------------------------------------------------------------------
# durable state: one reconciliation queue + the intent outbox
# ---------------------------------------------------------------------------

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS reconciliation_queue (
  id INTEGER PRIMARY KEY,
  rkey TEXT NOT NULL UNIQUE,
  subject_ref TEXT NOT NULL,
  target_id TEXT,
  old_refs TEXT NOT NULL,
  new_refs TEXT NOT NULL,
  kind TEXT NOT NULL,
  evidence TEXT NOT NULL,
  state TEXT NOT NULL,
  resolution TEXT,
  owner_turn_ref TEXT,
  version TEXT NOT NULL,
  ask_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS routing_outbox (
  intent_id TEXT PRIMARY KEY,
  source_event_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  delivery_key TEXT NOT NULL,
  destination TEXT NOT NULL,
  slot TEXT,
  target_id TEXT,
  subject TEXT,
  binding_version TEXT,
  state TEXT NOT NULL,
  reason TEXT,
  text TEXT NOT NULL,
  expires_at TEXT,
  verification TEXT,
  refs TEXT NOT NULL DEFAULT '[]',
  recon_id INTEGER,
  observers TEXT NOT NULL DEFAULT '[]',
  history TEXT NOT NULL DEFAULT '[]',
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS routing_outbox_event ON routing_outbox(source_event_id)",
)
TERMINAL_STATES = ("referenced", "no_consolidate")


def init_tables(conn):
    for s in SCHEMA:
        conn.execute(s)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(routing_outbox)")}
    for name in ("expires_at", "verification"):
        if name not in columns:
            conn.execute("ALTER TABLE routing_outbox ADD COLUMN " + name + " TEXT")
    _migrate_legacy_outbox(conn)


def _migrate_legacy_outbox(conn):
    """Detect a slice-1 consolidation_outbox (DGN-2010) and carry its rows in
    as intents, once. The legacy table is left as is; pending legacy rows are
    never erased by the migration."""
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='consolidation_outbox'").fetchone()
    if not has:
        return 0
    cols = {r[1] for r in conn.execute("PRAGMA table_info(consolidation_outbox)")}
    need = {"id", "destination", "source_ref", "item_id", "state", "reason", "text",
            "created_at", "updated_at"}
    if not need <= cols:
        return 0
    n = 0
    for r in conn.execute("SELECT id,destination,source_ref,item_id,state,reason,text,"
                          "created_at,updated_at FROM consolidation_outbox").fetchall():
        iid = "legacy-outbox:%s" % r[0]
        cur = conn.execute(
            "INSERT OR IGNORE INTO routing_outbox(intent_id,source_event_id,item_id,"
            "delivery_key,destination,state,reason,text,history,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (iid, "legacy:" + str(r[2]), str(r[3]), delivery_key(iid, r[1], "legacy"),
             r[1], r[4], r[5], r[6], json.dumps([{"migrated_from": "consolidation_outbox"}]),
             r[7], r[8]))
        n += cur.rowcount
    return n


def _j(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def recon_enqueue(conn, subject_ref, target_id, old_refs, new_refs, kind, evidence,
                  version, now):
    """Open a conflict once. The same disagreement (kind, subject, target,
    old and new refs) returns its existing row id and never changes its
    state: a retry or a replay neither re-asks nor resolves it."""
    if kind not in RECON_KINDS:
        raise ValueError("kind must be one of %s" % ",".join(RECON_KINDS))
    rkey = "rk-" + _h(kind, subject_ref, target_id or "", _j(sorted(old_refs)),
                      _j(sorted(new_refs)))
    row = conn.execute("SELECT id FROM reconciliation_queue WHERE rkey=?", (rkey,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO reconciliation_queue(rkey,subject_ref,target_id,old_refs,new_refs,kind,"
        "evidence,state,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (rkey, subject_ref, target_id, _j(list(old_refs)), _j(list(new_refs)), kind,
         _j(list(evidence)), "open", _j(version or {}), now, now))
    return cur.lastrowid


def _recon_state(conn, rid):
    row = conn.execute("SELECT state FROM reconciliation_queue WHERE id=?", (rid,)).fetchone()
    if not row:
        raise KeyError("no reconciliation row %s" % rid)
    return row[0]


def recon_mark_asked(conn, rid, ask_ref, now):
    """open/deferred -> asked (the owning agent asked about the factual
    disagreement in a relevant turn)."""
    if _recon_state(conn, rid) not in ("open", "deferred"):
        return False
    conn.execute("UPDATE reconciliation_queue SET state='asked', ask_ref=?, updated_at=? "
                 "WHERE id=?", (ask_ref, now, rid))
    return True


def recon_defer(conn, rid, now):
    if _recon_state(conn, rid) not in ("open", "asked"):
        return False
    conn.execute("UPDATE reconciliation_queue SET state='deferred', updated_at=? WHERE id=?",
                 (now, rid))
    return True


def recon_resolve(conn, rid, resolution, owner_turn_ref, current_version, now,
                  owner_turn_ok=None):
    """Resolve only against a real owner turn and the CURRENT record
    versions. Returns (True, None) or (False, why); a refusal leaves the row
    as it was. There is no time-based path: nothing here reads an age."""
    row = conn.execute("SELECT state,version FROM reconciliation_queue WHERE id=?",
                       (rid,)).fetchone()
    if not row:
        return False, "no_such_row"
    if row[0] == "resolved":
        return False, "already_resolved"
    if not isinstance(resolution, str) or not resolution.strip():
        return False, "no_resolution"
    check = owner_turn_ok or default_owner_turn_ok
    if not owner_turn_ref or not check(owner_turn_ref):
        return False, "owner_turn_unverified"
    if json.loads(row[1]) != (current_version or {}):
        return False, "stale_record_version"
    conn.execute("UPDATE reconciliation_queue SET state='resolved', resolution=?, "
                 "owner_turn_ref=?, updated_at=? WHERE id=?",
                 (resolution, owner_turn_ref, now, rid))
    return True, None


def default_owner_turn_ok(ref, root=None):
    """An owner turn ref is a turn-ledger id (otl-N) the UserPromptSubmit
    hook wrote for a real owner prompt (why_owner_turn.ledger_rows)."""
    root = root or os.path.normpath(os.path.join(os.path.dirname(
        os.path.abspath(__file__)), ".."))
    lib = os.path.join(root, "routines", "lib")
    try:
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import why_owner_turn  # noqa: WPS433
        return any(r.get("id") == ref for r in why_owner_turn.ledger_rows())
    except Exception:
        return False


def recon_pending(conn, target_id=None):
    """Every unresolved conflict, visible to the owning agent whether or not
    the current turn is on its topic."""
    q = ("SELECT id,subject_ref,target_id,kind,state,old_refs,new_refs FROM "
         "reconciliation_queue WHERE state!='resolved'")
    args = ()
    if target_id is not None:
        q += " AND target_id=?"
        args = (target_id,)
    return [dict(zip(("id", "subject_ref", "target_id", "kind", "state", "old_refs",
                      "new_refs"), r)) for r in conn.execute(q + " ORDER BY id", args)]


def upsert_intent(conn, p, instance, now):
    """One row per intent. A second observation of the same event (a crew
    member, an extraction replay, a worker retry) joins the observers; a
    changed destination migrates the row (history kept, delivery key
    recomputed) and never leaves a second active delivery."""
    dk = delivery_key(p["intent_id"], p["destination"], p["binding_version"])
    row = conn.execute("SELECT destination,state,observers,history,delivery_key "
                       "FROM routing_outbox WHERE intent_id=?",
                       (p["intent_id"],)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO routing_outbox(intent_id,source_event_id,item_id,delivery_key,"
            "destination,slot,target_id,subject,binding_version,state,reason,text,refs,"
            "recon_id,observers,created_at,updated_at,expires_at,verification) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["intent_id"], p["event_id"], p["item_id"], dk, p["destination"], p["slot"],
             p["target"], p["subject"], p["binding_version"], p["state"], p["reason"],
             p["text"], _j(p["refs"]), p.get("recon_id"), _j([instance]), now, now,
             p.get("expires_at"), p.get("verification")))
        return "inserted"
    dest, state, observers, history, old_dk = row
    observers = json.loads(observers)
    if instance not in observers:
        observers.append(instance)
    history = json.loads(history)
    if dest != p["destination"] or old_dk != dk:
        history.append({"from": dest, "state": state, "delivery_key": old_dk, "at": now})
        conn.execute(
            "UPDATE routing_outbox SET destination=?,delivery_key=?,binding_version=?,"
            "state=?,reason=?,refs=?,recon_id=?,observers=?,history=?,updated_at=? "
            "WHERE intent_id=?",
            (p["destination"], dk, p["binding_version"], p["state"], p["reason"],
             _j(p["refs"]), p.get("recon_id"), _j(observers), _j(history), now,
             p["intent_id"]))
        return "migrated"
    if state in TERMINAL_STATES:
        conn.execute("UPDATE routing_outbox SET observers=?,updated_at=? WHERE intent_id=?",
                     (_j(observers), now, p["intent_id"]))
        return "kept"
    conn.execute("UPDATE routing_outbox SET state=?,reason=?,refs=?,recon_id=?,observers=?,"
                 "updated_at=? WHERE intent_id=?",
                 (p["state"], p["reason"], _j(p["refs"]), p.get("recon_id"),
                  _j(observers), now, p["intent_id"]))
    return "refreshed"


def persist(conn, plans, instance, now):
    """Durably record every non-memory plan row (intent + its conflict) in
    one transaction. The caller advances the watermark only after this and
    the memory writes returned."""
    init_tables(conn)
    out = {}
    try:
        for p in plans:
            if p["destination"] == "base":
                continue
            if p.get("recon"):
                r = p["recon"]
                p["recon_id"] = recon_enqueue(conn, r["subject_ref"], r["target_id"],
                                              r["old_refs"], r["new_refs"], r["kind"],
                                              r["evidence"], r["version"], now)
            k = upsert_intent(conn, p, instance, now)
            out[k] = out.get(k, 0) + 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return out


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
