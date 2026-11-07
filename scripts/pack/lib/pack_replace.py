#!/usr/bin/env python3
"""Installer replacement lifecycle: a new pack engine REPLACES the old one.

A contract pack whose manifest names `replacement_manifest` (a payload-relative
JSON inventory with coexistence false) retires every resource of its previous
engine during install. There is no coexistence and no rollback (forward-fix):
a failed retirement leaves the old engine's admission closed and the install
incomplete; re-running the install resumes from the persisted intent.

  begin   before the first replacement byte: persist transition intent, keep
          the previous ledger (old manifest), close the old engine's
          admission (config/packs/<id>.revocation.json, bridge-enforced).
  seat    before begin: a v1 program the pack's config-install preserves
          needs the recorded runtime timezone and owner ruling (seat-check).
  retire  first, before any retirement step: store the owner's approval
          through the kit writer and pass the pack's config-install
          --describe prerequisite check (prepare); a refusal leaves the old
          engine untouched. Then, after delivery, before the ledger is
          finalized: remove every
          inventory resource and every previously delivered file the new
          payload omits (moved under files/_archive, never bare-deleted),
          disable external schedules through routine-ctl.sh, drain admitted
          invocations and deferred workers, verify absence, and write the
          authenticated absence attestation the bridge binds into the kit's
          trainer_installation_closure/1.

Any skipped or failed step exits non-zero; pack_install.sh then refuses
completion. Every external effect goes through Host so components can be
exercised with fixtures.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time

INTENT_SCHEMA = "pack_replacement/1"
ATTESTATION_SCHEMA = "pack_replacement_absence/1"
KNOWN_KINDS = frozenset((
    "service_file", "routine", "skill_tree", "stored_task_action", "callback_route",
    "callback_execution", "arm_store", "generation_store", "producer_flag",
    "deferred_worker", "transport_and_deferred_output", "external_schedule",
    "stored_callback", "external_producer", "surface_registry", "session_ledger",
    "external_schedule_inventory"))
OWNED_SURFACES = ("service/", "routines/bundle/", ".claude/skills-bundle/")


class ReplaceError(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def mac(key, value):
    return hmac.new(key, canonical(value), hashlib.sha256).hexdigest()


def need(condition, reason):
    if not condition:
        raise ReplaceError(reason)


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def rel_ok(value):
    return (isinstance(value, str) and value and not value.startswith("/")
            and all(p not in ("", ".", "..") for p in value.split("/")))


PROCESS_LISTERS = (["ps", "-axww", "-o", "pid=,command="], ["pgrep", "-lf", "."])


def list_processes(run):
    """[(pid, argv, None)] from ps, else pgrep (ps is kernel-denied inside
    dispatch sandboxes while pgrep works). A failed listing is never read
    as "no processes": when no lister yields rows, raise."""
    errors = []
    for argv in PROCESS_LISTERS:
        try:
            result = run(argv, capture_output=True, text=True, check=False)
        except OSError as exc:
            errors.append(f"{argv[0]}: {exc}")
            continue
        rows = []
        for line in result.stdout.splitlines():
            pid, _, command = line.strip().partition(" ")
            if pid.isdigit():
                rows.append((int(pid), command.split(), None))
        if result.returncode == 0 and rows:
            return rows
        errors.append(f"{argv[0]}: exit {result.returncode} {result.stderr.strip()[:200]}")
    raise RuntimeError("cannot list processes: " + "; ".join(errors))


class Host:
    """Real effects. Tests substitute clock/sleep/processes/kill/run."""
    def __init__(self, root, bridge_dir):
        self.root = Path(root).resolve()
        self.bridge_dir = Path(bridge_dir).resolve()

    def clock(self):
        return int(time.time() * 1000)

    def sleep(self, seconds):
        time.sleep(seconds)

    def processes(self):
        """[(pid, argv, cwd-or-None)] of this user's processes."""
        rows = []
        proc = Path("/proc")
        if proc.is_dir():
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    raw = (entry / "cmdline").read_bytes()
                    cwd = os.readlink(entry / "cwd")
                except OSError:
                    continue
                argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
                rows.append((int(entry.name), argv, cwd))
            return rows
        return list_processes(subprocess.run)

    def kill(self, pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def run(self, argv, env=None):
        result = subprocess.run(argv, capture_output=True, text=True, cwd=str(self.root),
                                env=dict(os.environ, **(env or {})), timeout=60, check=False)
        return result.returncode, result.stdout + result.stderr

    def run_stdin(self, argv, text, env=None, timeout=60):
        """(rc, stdout, stderr): the kit prints its JSON on stdout only."""
        result = subprocess.run(argv, input=text, capture_output=True, text=True, cwd=str(self.root),
                                env=dict(os.environ, **env) if env else None, timeout=timeout, check=False)
        return result.returncode, result.stdout, result.stderr

    def scheduler_units(self):
        """[(name, label, argv, unit_path)] of this agent's scheduled units."""
        agent = agent_name(self.root)
        units = []
        la = Path(os.environ.get("ROUTINE_CTL_LA_DIR", str(Path.home() / "Library/LaunchAgents")))
        prefix = "com.telegram-skill-bot." + agent + "."
        for path in sorted(la.glob(prefix + "*.plist")) if la.is_dir() else []:
            try:
                value = plistlib.loads(path.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                raise ReplaceError("unreadable scheduler unit " + path.name)
            argv = list(value.get("ProgramArguments") or [value.get("Program", "")])
            units.append((path.name[len(prefix):-len(".plist")], value.get("Label", ""), argv, path))
        unit_dir = Path(os.environ.get("ROUTINE_CTL_UNIT_DIR", str(Path.home() / ".config/systemd/user")))
        uprefix = "dogany-" + agent + "-"
        for path in sorted(unit_dir.glob(uprefix + "*.service")) if unit_dir.is_dir() else []:
            argv = []
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("ExecStart="):
                    argv = line[len("ExecStart="):].split()
            units.append((path.name[len(uprefix):-len(".service")], path.stem, argv, path))
        return units

    def routine_ctl(self, verb, name):
        script = self.root / "routines/lib/routine-ctl.sh"
        need(script.is_file(), "routine-ctl.sh missing at instance root")
        return self.run(["bash", str(script), verb, name])[0]


def agent_name(root):
    try:
        for line in (Path(root) / ".instance.conf").read_text(encoding="utf-8").splitlines():
            if line.startswith("DOGANY_AGENT_NAME="):
                value = line.split("=", 1)[1].strip().strip('"')
                if value:
                    return value
    except OSError:
        pass
    return Path(root).name


def revocation_module(host):
    sys.path.insert(0, str(host.bridge_dir.parent))
    from bridge import pack_revocation
    return pack_revocation


def load_inventory(manifest_file, payload_dir):
    manifest = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
    rel = manifest.get("replacement_manifest")
    if rel is None:
        return manifest, None
    need(rel_ok(rel), "replacement_manifest must be a payload-relative path")
    path = Path(payload_dir) / rel
    need(path.is_file(), "replacement_manifest names an absent payload file: " + rel)
    inventory = json.loads(path.read_text(encoding="utf-8"))
    need(isinstance(inventory, dict) and inventory.get("coexistence") is False,
         "replacement inventory must declare coexistence false")
    need(inventory.get("pack") == manifest.get("id"), "replacement inventory names another pack")
    need(inventory.get("paths_relative_to") == "instance_root", "inventory paths must be instance-relative")
    resources = inventory.get("resources")
    need(isinstance(resources, list) and resources, "replacement inventory has no resources")
    ids = [r.get("id") for r in resources if isinstance(r, dict)]
    need(len(ids) == len(resources) and len(set(ids)) == len(ids), "inventory resource ids must be unique")
    for r in resources:
        need(r.get("kind") in KNOWN_KINDS, "unknown replacement resource kind: %r" % r.get("kind"))
        if "path" in r:
            need(rel_ok(r["path"]), "resource path escapes the instance: " + r["id"])
    return manifest, dict(inventory, digest=digest(inventory), rel=rel)


def of_kind(inventory, *kinds):
    return [r for r in inventory["resources"] if r["kind"] in kinds]


def paths(root, kind):
    base = Path(root) / "config/packs"
    return dict(intent=base / (kind + ".replacement.json"),
                previous=base / (kind + ".replacement-previous.files"),
                attestation=base / (kind + ".replacement-attestation.json"))


def recorded_version(root, pack):
    try:
        for line in (Path(root) / ".instance.conf").read_text(encoding="utf-8").splitlines():
            if line.startswith("DOGANY_PACKS="):
                for item in line.split("=", 1)[1].split(","):
                    name, _, version = item.partition("@")
                    if name == pack:
                        return version or "unknown"
    except OSError:
        pass
    return "unknown"


def begin(host, manifest_file, payload_dir):
    """Persist intent and close admission. Idempotent; never reopens."""
    manifest, inventory = load_inventory(manifest_file, payload_dir)
    if inventory is None:
        return None
    pack = manifest["id"]
    files = paths(host.root, pack)
    intent = None
    if files["intent"].exists():
        intent = json.loads(files["intent"].read_text(encoding="utf-8"))
        need(intent.get("schema") == INTENT_SCHEMA and intent.get("pack") == pack,
             "replacement intent record is damaged")
    if intent is None or intent.get("replacement_digest") != inventory["digest"]:
        ledger = host.root / "config/packs" / (pack + ".files")
        if not files["previous"].exists():
            if ledger.is_file():
                shutil.copyfile(ledger, files["previous"])
            else:
                files["previous"].write_text("# no prior ledger (pre-ledger install)\n", encoding="utf-8")
        intent = dict(schema=INTENT_SCHEMA, pack=pack, phase="retiring",
                      previous_version=intent["previous_version"] if intent else recorded_version(host.root, pack),
                      replacement_version=str(manifest.get("pack_version", "")),
                      replacement_digest=inventory["digest"], replacement_manifest=inventory["rel"],
                      started_at_ms=host.clock(), admission="closed")
        atomic_json(files["intent"], intent)
    revoke = revocation_module(host)
    prior = None
    path = revoke.record_path(host.root, pack)
    if path.exists():
        prior = json.loads(path.read_text(encoding="utf-8"))
    selector = [r for r in of_kind(inventory, "callback_execution")]
    record = dict(schema=revoke.SCHEMA, pack=pack, closed_at_ms=(prior or {}).get("closed_at_ms", host.clock()),
                  replacement_digest=inventory["digest"],
                  callback_prefixes=sorted({r["prefix"] for r in of_kind(inventory, "callback_route")}
                                           | set((prior or {}).get("callback_prefixes", []))),
                  arm_command_paths=sorted({p for r in selector for p in r["ownership_selector"]["arm_command_paths"]}
                                           | set((prior or {}).get("arm_command_paths", []))),
                  arm_command_keys=sorted({k for r in selector for k in r.get("arm_schema_command_keys", revoke.ARM_KEYS)}
                                          | set((prior or {}).get("arm_command_keys", [])) or set(revoke.ARM_KEYS)),
                  retired_arm_ids=sorted(set((prior or {}).get("retired_arm_ids", []))),
                  notice_producers=sorted(set((prior or {}).get("notice_producers", []))))
    revoke.write(host.root, record)
    return intent


class Retirement:
    def __init__(self, host, manifest_file, payload_dir, delivered_file, drain_deadline_ms=20000):
        self.host = host
        self.manifest, self.inventory = load_inventory(manifest_file, payload_dir)
        need(self.inventory is not None, "manifest declares no replacement")
        self.pack = self.manifest["id"]
        self.payload = Path(payload_dir)
        self.files = paths(host.root, self.pack)
        need(self.files["intent"].is_file(), "replacement intent missing: begin did not run")
        self.intent = json.loads(self.files["intent"].read_text(encoding="utf-8"))
        need(self.intent.get("replacement_digest") == self.inventory["digest"],
             "replacement intent belongs to another inventory")
        self.delivered = set()
        if delivered_file and Path(delivered_file).is_file():
            self.delivered = {l.strip() for l in Path(delivered_file).read_text(encoding="utf-8").splitlines()
                              if l.strip() and not l.startswith("#")}
        self.revoke = revocation_module(host)
        self.deadline = drain_deadline_ms
        self.archive = host.root / "files/_archive" / ("replace-%s-%d" % (self.pack, host.clock()))
        self.removed, self.replaced, self.evidence, self.log = set(), set(), {}, []

    def note(self, line):
        self.log.append(line)

    def move(self, rel):
        """Archive one instance path (file, link or directory). Failure blocks."""
        source = self.host.root / rel
        if not (source.exists() or source.is_symlink()):
            return False
        target = self.archive / rel
        if target.exists() or target.is_symlink():
            target = target.with_name(target.name + ".dup-" + secrets.token_hex(3))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        except OSError as error:
            raise ReplaceError("archive of %s failed: %s" % (rel, error))
        need(not (source.exists() or source.is_symlink()), "archive left %s in place" % rel)
        self.note("archived " + rel)
        return True

    # -- files -----------------------------------------------------------
    def replaced_files(self):
        for r in of_kind(self.inventory, "service_file", "routine"):
            path = self.host.root / r["path"]
            if r["disposition"] == "replace":
                need(path.is_file(), "replacement missing after delivery: " + r["path"])
                need(file_sha(path) == r["sha256"], "previous engine bytes remain at " + r["path"])
                self.replaced.add(r["path"])
            else:
                self.move(r["path"])
                self.removed.add(r["path"])

    def omitted_files(self):
        """Previously delivered pack files the new payload no longer ships."""
        previous = self.files["previous"]
        rows = [l.strip() for l in previous.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.startswith("#")] if previous.is_file() else []
        if not rows:
            self.note("no previous ledger: omitted-file diff limited to the replacement inventory")
        for rel in rows:
            if rel in self.delivered or not rel.startswith(OWNED_SURFACES) or not rel_ok(rel):
                continue
            if self.move(rel):
                self.removed.add(rel)

    def skill_trees(self):
        for r in of_kind(self.inventory, "skill_tree"):
            need(r["disposition"] == "remove_owned" and r["path"].startswith(".claude/skills-bundle/"),
                 "unsupported skill_tree disposition: " + r["id"])
            name = r["path"].rsplit("/", 1)[1]
            shipped = self.payload / "skills-bundle" / name
            keep = {p.relative_to(shipped).as_posix() for p in shipped.rglob("*") if p.is_file()} \
                if shipped.is_dir() and r.get("preserve_omitted_files") is False else None
            tree = self.host.root / r["path"]
            if tree.is_symlink():
                self.move(r["path"])
                self.removed.add(r["path"])
            elif tree.is_dir():
                for path in sorted(tree.rglob("*"), reverse=True):
                    rel = path.relative_to(tree).as_posix()
                    if path.is_file() or path.is_symlink():
                        if keep is None or rel not in keep:
                            self.move(r["path"] + "/" + rel)
                            self.removed.add(r["path"] + "/" + rel)
                    elif path.is_dir() and not any(path.iterdir()):
                        path.rmdir()
                if keep is None and tree.is_dir() and not any(tree.iterdir()):
                    tree.rmdir()
            need(not (tree / "SKILL.md").exists(), "retired skill still loadable: " + r["path"])
            # A placeholder tree the new payload still declares keeps its
            # activation link (install evidence anchors it; with no SKILL.md
            # nothing loads). A tree that is gone takes its link with it.
            link = ".claude/skills/" + name
            if not tree.is_dir() and self.move(link):
                self.removed.add(link)

    def stores(self):
        retired = set()
        for r in of_kind(self.inventory, "arm_store"):
            directory = self.host.root / r["path"]
            for path in sorted(directory.iterdir()) if directory.is_dir() else []:
                if not path.is_file():
                    continue
                try:
                    arm = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise ReplaceError("unclassifiable arm (unknown command binding): " + path.name)
                keys = [k for k in self.revoke.ARM_KEYS if k in arm] if isinstance(arm, dict) else None
                need(keys is not None and all(isinstance(arm[k], list) for k in keys),
                     "unclassifiable arm (unknown command binding): " + path.name)
                if self.revoke.arm_owner(self.host.root, arm, None, [self.record()]):
                    self.move(r["path"] + "/" + path.name)
                    retired.add(path.name)
        for r in of_kind(self.inventory, "generation_store", "producer_flag"):
            target = self.host.root / r["path"]
            if r["kind"] == "generation_store" and target.is_dir():
                for path in sorted(target.iterdir()):
                    self.move(r["path"] + "/" + path.name)
            elif self.move(r["path"]):
                self.removed.add(r["path"])
        for r in of_kind(self.inventory, "deferred_worker"):
            if r.get("path"):
                directory = self.host.root / r["path"]
                for path in sorted(directory.iterdir()) if directory.is_dir() else []:
                    self.move(r["path"] + "/" + path.name)
        if retired:
            record = self.record()
            record["retired_arm_ids"] = sorted(set(record.get("retired_arm_ids", [])) | retired)
            self.revoke.write(self.host.root, record)
        self.evidence["callbacks"] = dict(retired_arms=sorted(retired),
                                          prefixes=self.record()["callback_prefixes"])

    def record(self):
        return json.loads(self.revoke.record_path(self.host.root, self.pack).read_text(encoding="utf-8"))

    # -- schedules -------------------------------------------------------
    def schedules(self):
        match = set()
        for r in of_kind(self.inventory, "external_schedule_inventory"):
            need(r["disposition"] == "remove_owned", "unsupported schedule inventory disposition")
            match |= set(r["match_executable_paths"])
        match |= {p for p in self.removed if p.startswith("routines/bundle/")}
        retained = {r["label"] for r in of_kind(self.inventory, "external_schedule")
                    if r["disposition"] == "retain_and_reconcile"}
        disabled = []
        for name, label, argv, unit in self.host.scheduler_units():
            if not any(self.host_relative(a) in match for a in argv):
                continue
            need(name not in retained, "a retained schedule runs a retired path: " + name)
            rc = self.host.routine_ctl("disable", name)
            need(rc == 0, "routine-ctl disable %s failed (rc %d)" % (name, rc))
            status = self.host.routine_ctl("status", name)
            need(status == 3, "schedule %s still loaded after disable" % name)
            disabled.append(name)
        for path in sorted((self.host.root / "routines").glob("*.plist")):
            try:
                value = plistlib.loads(path.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            if any(self.host_relative(a) in match for a in value.get("ProgramArguments") or []):
                rel = "routines/" + path.name
                self.move(rel)
                self.removed.add(rel)
        healthkit = {}
        for r in of_kind(self.inventory, "external_schedule"):
            need(r["disposition"] == "retain_and_reconcile", "unsupported external schedule: " + r["id"])
            routine = self.host.root / r["routine"]
            need(r["routine"] in self.replaced or routine.is_file(), "retained schedule lost its routine: " + r["id"])
            healthkit[r["label"]] = "scheduled" if self.host.routine_ctl("status", r["label"]) == 0 else "not_scheduled"
        self.evidence["schedules"] = dict(disabled=sorted(disabled))
        self.evidence["healthkit"] = healthkit

    def host_relative(self, value):
        if not isinstance(value, str) or not value:
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            return value if rel_ok(value) else None
        try:
            return candidate.resolve().relative_to(self.host.root).as_posix()
        except (ValueError, OSError):
            return None

    # -- stored actions and live targets ----------------------------------
    def stored_actions(self):
        done = []
        for r in of_kind(self.inventory, "stored_task_action"):
            need(r["disposition"] == "replace" and rel_ok(r["reconciler"]), "unsupported stored action: " + r["id"])
            reconciler = self.host.root / r["reconciler"]
            need(reconciler.is_file(), "stored action reconciler missing: " + r["reconciler"])
            rc, output = self.host.run([sys.executable, str(reconciler), "--root", str(self.host.root)])
            need(rc == 0, "stored action %s not reconciled (rc %d): %s" % (r["id"], rc, output.strip()[:200]))
            done.append(r["id"])
        self.evidence["stored_actions"] = done

    def surfaces(self):
        """Exact legacy keyboard targets for the bridge transition inventory."""
        directories = [r["path"] for r in of_kind(self.inventory, "surface_registry")]
        try:
            self.evidence["inventory"] = self.revoke.legacy_targets(self.host.root, directories)
        except ValueError as error:
            raise ReplaceError(str(error))

    # -- drain -----------------------------------------------------------
    def workers(self):
        """[(pid, argv)] still running a retired producer for this root."""
        root = str(self.host.root)
        producers = set()
        for r in of_kind(self.inventory, "deferred_worker", "transport_and_deferred_output"):
            for value in [r.get("producer")] + list(r.get("producers", [])) + list(r.get("argv", [])[:1]):
                if value:
                    producers.add(value.split(":", 1)[0])
        producers |= set(self.record()["arm_command_paths"])
        chains = [r for r in of_kind(self.inventory, "deferred_worker") if r.get("captured_command")]
        mine = {os.getpid(), os.getppid()}
        found = []
        for pid, argv, cwd in self.host.processes():
            if pid in mine:
                continue
            text = " ".join(argv)
            here = cwd == root or root + "/" in text
            if not here:
                continue
            rels = {self.host_relative(a) for a in argv} | {self.host_relative(w) for a in argv for w in a.split()}
            if rels & producers:
                found.append((pid, argv))
                continue
            for r in chains:
                head, *rest = r["captured_command"]
                if re.search(r"(^|[\s/;&|(])%s(\s|$)" % re.escape(head), text) and all(
                        part in text for part in rest):
                    found.append((pid, argv))
                    break
        return found

    def drain(self):
        start = self.host.clock()
        terminated = []
        while True:
            invocations = self.revoke.admitted(self.host.root, self.pack)
            workers = self.workers()
            for pid, argv in workers:
                if pid not in terminated:
                    self.host.kill(pid)
                    terminated.append(pid)
            if not invocations and not workers:
                break
            need(self.host.clock() - start < self.deadline,
                 "drain incomplete: %d admitted invocation(s), %d deferred worker(s) still running"
                 % (len(invocations), len(workers)))
            self.host.sleep(0.2)
        self.evidence["drain"] = dict(admitted_remaining=0)
        self.evidence["deferred"] = dict(terminated=sorted(terminated))

    # -- verdict ---------------------------------------------------------
    def remaining(self):
        left = sorted(p for p in self.removed if (self.host.root / p).exists() or (self.host.root / p).is_symlink())
        for r in of_kind(self.inventory, "service_file", "routine"):
            path = self.host.root / r["path"]
            if r["disposition"] == "replace" and (not path.is_file() or file_sha(path) != r["sha256"]):
                left.append(r["path"])
        return sorted(set(left))

    def key(self):
        path = self.revoke.installer_key_path(self.host.root)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
        need(not path.is_symlink() and (path.stat().st_mode & 0o077) == 0, "installer key must be private")
        return path.read_bytes()

    def run(self):
        self.replaced_files()
        self.omitted_files()
        self.skill_trees()
        self.stores()
        self.schedules()
        self.stored_actions()
        self.surfaces()
        self.drain()
        left = self.remaining()
        need(not left, "previous engine resources remain: " + ", ".join(left))
        previous = sorted(self.removed | self.replaced)
        need(previous, "replacement retired nothing: refusing an empty previous manifest")
        facts = {name: dict(complete=True, evidence_digest=digest(self.evidence.get(name)),
                            evidence=self.evidence.get(name))
                 for name in ("drain", "deferred", "callbacks", "healthkit")}
        value = dict(schema=ATTESTATION_SCHEMA, pack=self.pack, installed_root=str(self.host.root),
                     previous_version=self.intent["previous_version"],
                     replacement_version=self.intent["replacement_version"],
                     replacement_digest=self.inventory["digest"],
                     replacement_manifest=self.inventory["rel"],
                     previous_manifest=previous, manifest_digest=digest(previous), remaining_paths=left,
                     checked_at=self.host.clock(), facts=facts, inventory=self.evidence["inventory"],
                     schedules=self.evidence["schedules"], stored_actions=self.evidence["stored_actions"])
        value["auth"] = dict(mac=mac(self.key(), value))
        atomic_json(self.files["attestation"], value)
        self.revoke.verify_attestation(self.host.root, self.pack, self.key())
        self.intent.update(phase="retired", attestation_digest=digest(value), retired_at_ms=value["checked_at"])
        atomic_json(self.files["intent"], self.intent)
        return value


KIT_COMMAND = "service/kit_life/session-command"
# The tree this installer runs from (framework source / channel payload), not the instance.
REPO = Path(__file__).resolve().parents[3]
SESSION_CONFIGURATION = "kit_session_configuration/1"


def session_configuration(manifest_file):
    """The incoming manifest's kit session declaration, or None when absent.

    Only a pack declaring session_configuration (kit_session_configuration/1)
    has a kit config-install step; a declaration of any other shape refuses.
    """
    manifest = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
    value = manifest.get("session_configuration") if isinstance(manifest, dict) else None
    if value is None:
        return None
    need(isinstance(value, dict) and value.get("schema") == SESSION_CONFIGURATION,
         "session_configuration must declare schema " + SESSION_CONFIGURATION)
    return value


def last_line(text):
    return (text.strip().splitlines() or [""])[-1]


def bridge_module(host, name):
    sys.path.insert(0, str(host.bridge_dir.parent))
    import importlib
    return importlib.import_module("bridge." + name)


def native_capabilities(host):
    """The framework's own API/feature advertisement for this installed root."""
    activation = bridge_module(host, "session_activation")
    surface = bridge_module(host, "live_surface")
    apis = surface.m2_services(host.root).get("surface_apis")
    # The bridge advertises nothing either when its pinned kit codec refuses.
    need(apis, "framework session services unavailable for this root (pinned kit artifact codec refused)")
    return activation.capabilities(host.root, apis, None)


def session_entrypoint(host, declaration):
    """The pack's declared config-install entry point as an absolute path, or None.

    A relative path with no "..", resolving inside the installed root to a
    regular executable file; anything else refuses.
    """
    value = declaration.get("entrypoint")
    if value is None:
        return None

    def invalid(why):
        return ReplaceError("session_configuration.entrypoint invalid: %s (%r)" % (why, value))

    if not isinstance(value, str) or not value:
        raise invalid("not a non-empty string")
    rel = Path(value)
    if rel.is_absolute():
        raise invalid("absolute path")
    if ".." in rel.parts:
        raise invalid("contains '..'")
    path = host.root / rel
    root = host.root.resolve()
    if root not in path.resolve().parents:
        raise invalid("resolves outside the installed root")
    if not path.is_file():
        raise invalid("not a regular file")
    if not os.access(path, os.X_OK):
        raise invalid("not executable")
    return path


def config_wrapper(attestation, proof):
    """The kit's four-field stdin object, stage one (null closure).

    challenge / capability_receipt come from the framework's installer-seat
    prober (session_activation.install_probe); legacy_retirement is derived
    from the installer's absence attestation; surfaces stay empty until the
    activation seat publishes the closure (PackTransitionIO.publish_closure).
    """
    facts = attestation["facts"]
    done = lambda *names: all(facts[n].get("complete") is True for n in names)
    return dict(challenge=proof["challenge"], capability_receipt=proof["capability_receipt"],
                legacy_retirement=dict(capability_receipt_id=proof["capability_receipt"]["receipt_id"],
                                       routes_revoked=done("callbacks"),
                                       workers_fenced=done("drain", "deferred"), surfaces=[]),
                installation_closure=None)


def need_command(rc, out, err, what):
    """Refuse on a nonzero exit; exit 3 surfaces the pack's INSTANCE_FACTS line verbatim."""
    if rc == 0:
        return
    if rc == 3:
        lines = [l for l in (err or "").splitlines() if l.startswith("INSTANCE_FACTS:")]
        raise ReplaceError("%s refused (rc 3): %s" % (what, lines[-1] if lines else last_line(err or out)))
    raise ReplaceError("kit %s refused (rc %d): %s" % (what, rc, last_line(err or out)))


# ---- installer preparation seat (DGN-2085 O4) ------------------------------
# A v1 instance upgrading to a pack whose config-install entrypoint preserves
# v1 programs needs two instance facts the pack must never guess: the runtime
# IANA timezone and the owner-approved source policy reference, plus the
# owner's ruling stored as typed source_policy_approval/1 records through the
# kit's own writer (session_program.store_source_policy). Both come from
# durable instance records; nothing here is a constant.
V1_PROGRAM = "files/program/program.yaml"
INSTANCE_ENV = ".telegram_bot/.env"
RULING_SCHEMA = "owner_source_policy_ruling/1"
RULING_DECISION = "preserve_v1_rules"
APPROVAL_SCHEMA = "source_policy_approval/1"
# The v1 upgrade producer replays every slot's v1 reader (90s bound each).
SEAT_TIMEOUT = 900


def ruling_path(root, pack):
    return Path(root) / "config/packs" / (pack + ".source-policy-ruling.json")


def runtime_timezone(root):
    """The instance's recorded runtime timezone (TZ= in the bridge .env, as mint wrote it)."""
    path = Path(root) / INSTANCE_ENV
    value = None
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("TZ="):
                value = line.split("=", 1)[1].strip().strip("\"'")
    need(value, "no recorded runtime timezone (TZ= in %s)" % INSTANCE_ENV)
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(value)
    except (ValueError, KeyError, OSError):
        raise ReplaceError("recorded runtime timezone %r (%s) is not an IANA zone" % (value, INSTANCE_ENV))
    return value


def check_ruling(value, pack):
    from datetime import datetime
    need(isinstance(value, dict) and value.get("schema") == RULING_SCHEMA and value.get("pack") == pack,
         "owner ruling record is not %s for %s" % (RULING_SCHEMA, pack))
    need(value.get("decision") == RULING_DECISION, "owner ruling is not " + RULING_DECISION)
    for key in ("policy_ref", "ruling_ref", "ruled_at"):
        need(isinstance(value.get(key), str) and value[key].strip(), "owner ruling lacks " + key)
    try:
        ruled = datetime.fromisoformat(value["ruled_at"])
    except ValueError:
        raise ReplaceError("owner ruling ruled_at is not an ISO-8601 time")
    need(ruled.tzinfo is not None, "owner ruling ruled_at carries no UTC offset")
    return value


def load_ruling(root, pack):
    path = ruling_path(root, pack)
    need(path.is_file(), "no recorded owner ruling (%s)" % path.relative_to(root).as_posix())
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise ReplaceError("owner ruling record unreadable: " + path.name)
    return check_ruling(value, pack)


def record_ruling(root, pack, policy_ref, ruled_at, ruling_ref):
    """Persist the owner's preserve ruling (0600). An existing different record refuses."""
    value = check_ruling(dict(schema=RULING_SCHEMA, pack=pack, decision=RULING_DECISION, policy_ref=policy_ref,
                              ruled_at=ruled_at, ruling_ref=ruling_ref), pack)
    path = ruling_path(root, pack)
    if path.exists():
        need(json.loads(path.read_text(encoding="utf-8")) == value,
             "a different owner ruling is already recorded at " + path.name)
        return path, False
    atomic_json(path, value)
    os.chmod(path, 0o600)
    return path, True


def preparation(host, pack, manifest_file):
    """The seat inputs for a v1-preserving config-install, or None when none apply.

    Applies when the incoming manifest declares a pack config-install
    entrypoint and the instance carries a v1 program. Pure reads: safe before
    the first replacement byte. A missing input refuses with the plain reason.
    """
    declaration = session_configuration(manifest_file)
    if declaration is None or declaration.get("entrypoint") is None:
        return None
    if not (host.root / V1_PROGRAM).is_file():
        return None
    reasons, seat = [], {}
    for name, read in (("timezone", lambda: runtime_timezone(host.root)),
                       ("ruling", lambda: load_ruling(host.root, pack))):
        try:
            seat[name] = read()
        except ReplaceError as error:
            reasons.append(str(error))
    need(not reasons, "your v1 programs cannot be carried into the new engine yet: %s"
         " -- the current workout engine was left as it is" % "; ".join(reasons))
    seat["policy_ref"] = seat["ruling"]["policy_ref"]
    return seat


def seat_args(seat):
    return [] if seat is None else ["--timezone", seat["timezone"], "--policy-ref", seat["policy_ref"]]


def store_approval(root, agent, entry_dir, timezone, ruling):
    """Run inside the instance's own interpreter context (see prepare()).

    The pack's upgrade producer builds the candidate facts; the approval binds
    the recorded ruling to them and the kit writer stores it in a caller-owned
    transaction. A re-run reuses the stored version, so the writer's own
    equality check makes it idempotent.
    """
    from datetime import datetime
    import sqlite3
    root = Path(root)
    sys.path[:0] = [str(root / "database"), str(entry_dir)]
    from preserve_program import prepare_upgrade
    import session_program
    manifest = prepare_upgrade(root, agent, timezone, ruling["policy_ref"])
    facts = json.loads((root / manifest).with_name("facts.json").read_text(encoding="utf-8"))
    members = sorted(m for m, b in (facts.get("history_bindings") or {}).items()
                     if isinstance(b, dict) and b.get("owning_agent") == agent)
    need(len(members) == 1, "candidate facts bind %d members to %s, need exactly one" % (len(members), agent))
    ruled = datetime.fromisoformat(ruling["ruled_at"])
    with sqlite3.connect(root / "database/kit_life.db") as conn:
        conn.execute("BEGIN IMMEDIATE")
        stored = conn.execute("SELECT version FROM training_policy_bundles WHERE id=?",
                              (ruling["policy_ref"],)).fetchone()
        version = stored[0] if stored else conn.execute(
            "SELECT coalesce(max(version),0)+1 FROM training_policy_bundles WHERE owner_id=?",
            (members[0],)).fetchone()[0]
        approval = dict(schema=APPROVAL_SCHEMA, decision=ruling["decision"], member_id=members[0],
                        owning_agent=agent, policy_ref=ruling["policy_ref"], version=version,
                        effective_at=int(ruled.timestamp() * 1000), source_date=ruled.date().isoformat(),
                        ruling_ref=ruling["ruling_ref"],
                        source_digest=facts["provenance"]["from_v1"]["source_digest"],
                        calculators={ref: {k: c[k] for k in ("version", "code_digest", "rule_digest")}
                                     for ref, c in facts["calculators"].items()})
        bundle = session_program.store_source_policy(conn, facts, approval)
    return dict(manifest=str(manifest), member=members[0], bundle_id=bundle["id"], version=version,
                created=stored is None, records=len(bundle["rules"]),
                sources=[r["source_ref"] for r in bundle["rules"]], ruling_ref=ruling["ruling_ref"])


def prepare(host, pack, manifest_file):
    """Before ANY retirement step: seat handoff, then the pack's --describe prerequisite check.

    Returns None when no session_configuration is declared. Otherwise stores
    the owner's approval through the kit writer (when the seat applies) and
    runs config-install --describe with the seat inputs; any refusal raises
    with the previous engine untouched.
    """
    declaration = session_configuration(manifest_file)
    if declaration is None:
        return None
    seat = preparation(host, pack, manifest_file)
    command = session_entrypoint(host, declaration)
    if command is None:
        command = host.root / KIT_COMMAND
        need(command.is_file() and os.access(command, os.X_OK),
             "pack declares session_configuration but the installed kit lacks " + KIT_COMMAND)
    stored = None
    if seat is not None:
        env = dict(LIFEKIT_CORE_PY=str(host.root / "database/kit_life.py"), INSTANCE_ROOT=str(host.root),
                   PYTHONDONTWRITEBYTECODE="1")
        rc, out, err = host.run_stdin([sys.executable, "-B", str(Path(__file__).resolve()), "seat-store",
                                       "--root", str(host.root), "--entry-dir", str(command.parent)],
                                      json.dumps(dict(timezone=seat["timezone"], ruling=seat["ruling"])), env=env,
                                      timeout=SEAT_TIMEOUT)
        need(rc == 0, "owner source policy approval not stored (rc %d): %s" % (rc, last_line(err or out)))
        stored = json.loads(out)
    rc, out, err = host.run_stdin([str(command), "config-install", "--describe"] + seat_args(seat), "",
                                  timeout=SEAT_TIMEOUT if seat else 60)
    need_command(rc, out, err, "config-install --describe")
    return dict(seat=seat, approval=stored)


def retire(host, manifest_file, payload_dir, delivered_file, report=print):
    """prepare() first (fail-closed: a refusal precedes every retirement step), then Retirement."""
    prepared = prepare(host, json.loads(Path(manifest_file).read_text(encoding="utf-8"))["id"], manifest_file)
    if prepared is not None:
        approval = prepared["approval"]
        if approval:
            report("replace: owner source policy approval %s via kit writer (bundle %s v%d, %d record(s), %s)"
                   % ("stored" if approval["created"] else "already stored", approval["bundle_id"],
                      approval["version"], approval["records"], approval["ruling_ref"]))
        report("replace: config-install --describe prerequisite check passed")
    retirement = Retirement(host, manifest_file, payload_dir, delivered_file)
    try:
        value = retirement.run()
    finally:
        for line in retirement.log:
            report("replace: " + line)
    report("replace: retired %d path(s), %d replaced; absence attested (%s)"
           % (len(retirement.removed), len(retirement.replaced), digest(value)))
    return value


def config_install(host, pack, manifest_file, capabilities=None, installer_root=None):
    """Stage 1: run the INSTALLED kit's config-install with a framework proof.

    When session_configuration.entrypoint is declared, that pack entry point is
    run instead of the kit command (it does the instance-facts step, then execs
    the kit with its own --manifest; this installer never passes --manifest).

    Returns None (skipped) when the incoming manifest declares no kit session
    configuration. Otherwise: --describe, measure the native capability
    receipt over exactly those inputs, then feed the four-field wrapper, and
    record the measured installer digest in the instance (INSTALLER_RECORD,
    0600) for the runtime capability attestation. Never passes --rotate (an
    existing key is kept). Any failure raises, so the installer refuses
    completion and nothing is activated.
    """
    declaration = session_configuration(manifest_file)
    if declaration is None:
        return None
    attestation = revocation_module(host).verify_attestation(host.root, pack)
    seat = preparation(host, pack, manifest_file)
    command = session_entrypoint(host, declaration)
    if command is None:
        command = host.root / KIT_COMMAND
        need(command.is_file() and os.access(command, os.X_OK),
             "pack declares session_configuration but the installed kit lacks " + KIT_COMMAND)
    rc, out, err = host.run_stdin([str(command), "config-install", "--describe"] + seat_args(seat), "",
                                  timeout=SEAT_TIMEOUT if seat else 60)
    need_command(rc, out, err, "config-install --describe")
    try:
        described = json.loads(out)
    except ValueError as error:
        raise ReplaceError("kit config-install --describe unreadable: %s" % error)
    activation = bridge_module(host, "session_activation")
    try:
        proof = activation.install_probe(host.root, described, clock=host.clock,
                                         installer_files={rel: str(Path(installer_root or REPO) / rel)
                                                          for rel in activation.INSTALLER_FILES},
                                         current_capabilities=capabilities or (lambda: native_capabilities(host)))
    except activation.Refusal as error:
        raise ReplaceError("native capability probe refused (rc %d): %s" % (error.rc, error))
    wrapper = config_wrapper(attestation, proof)
    rc, out, err = host.run_stdin([str(command), "config-install"] + seat_args(seat),
                                  json.dumps(wrapper, sort_keys=True), timeout=SEAT_TIMEOUT if seat else 60)
    need_command(rc, out, err, "config-install")
    target = host.root / "config/kit_life/session-v2.json"
    need(target.is_file() and not target.is_symlink() and (target.stat().st_mode & 0o777) == 0o600,
         "session-v2.json missing or not mode 0600 after config-install")
    # The instance never carries this installer; runtime attestation reads its measurement.
    try:
        activation.write_installer_record(host.root, proof["installer_record"], proof["capability_receipt"])
    except activation.Refusal as error:
        raise ReplaceError("installer artifact record refused (rc %d): %s" % (error.rc, error))
    return target


def activation_keys(host, pack, manifest_file):
    """Stage 1b: provision every key the kit activation authority reads.

    Declared by session_configuration.activation.authority_id. Runs here, at
    the installer seat (after config-install, before the ledger is finalized
    and before any activation), because the installer owns the live-surface
    data dir and the pack ledger. Nothing else writes these keys: the kit
    and live_surface only read publishers/<pack>.key, coordinator.key and
    recovery-authorities/<authority_id>.key. Returns None when nothing is
    declared, else [(name, rel, created, ledger)]; ledger is True for the
    pack-owned keys (publisher, recovery), recorded in config/packs/<id>.files
    so retirement/uninstall removes them. coordinator.key is instance-wide
    (every pack's maintenance receipts) and is never ledgered to one pack.
    """
    declaration = session_configuration(manifest_file)
    activation = (declaration or {}).get("activation")
    if activation is None or isinstance(activation, dict) and "authority_id" not in activation:
        return None
    need(isinstance(activation, dict), "session_configuration.activation must be an object")
    module = bridge_module(host, "session_activation")
    try:
        keys = module.provision_activation_keys(host.root, pack, activation["authority_id"])
    except module.Refusal as error:
        raise ReplaceError("activation key refused (rc %d): %s" % (error.rc, error))
    return [(name, path.relative_to(host.root).as_posix(), created, name != "coordinator")
            for name, path, created in keys]


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("verb", choices=("declared", "seat-check", "begin", "retire", "verify", "config-install",
                                         "activation-keys", "record-ruling", "seat-store"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--payload")
    parser.add_argument("--bridge", default=str(Path(__file__).resolve().parents[3] / "agents/.template/bridge"))
    parser.add_argument("--delivered")
    parser.add_argument("--pack")
    parser.add_argument("--policy-ref")
    parser.add_argument("--ruled-at")
    parser.add_argument("--ruling-ref")
    parser.add_argument("--entry-dir")
    args = parser.parse_args(argv)
    host = Host(args.root, args.bridge)
    try:
        if args.verb == "seat-store":
            # Internal: the instance-context half of prepare(); stdin carries the seat.
            seat = json.loads(sys.stdin.read())
            print(json.dumps(store_approval(host.root, agent_name(host.root), args.entry_dir,
                                            seat["timezone"], seat["ruling"]), sort_keys=True))
            return 0
        if args.verb == "record-ruling":
            path, created = record_ruling(host.root, args.pack, args.policy_ref, args.ruled_at, args.ruling_ref)
            print("replace: owner ruling %s (%s)" % ("recorded" if created else "already recorded",
                                                     path.relative_to(host.root).as_posix()))
            return 0
        if args.verb == "seat-check":
            pack = json.loads(Path(args.manifest).read_text(encoding="utf-8"))["id"]
            seat = preparation(host, pack, args.manifest)
            print("replace: preparation seat -- " + ("not needed (no v1 program to preserve)" if seat is None else
                  "timezone %s, owner ruling %s (policy %s)" % (seat["timezone"], seat["ruling"]["ruling_ref"],
                                                                seat["policy_ref"])))
            return 0
        if args.verb == "declared":
            _, inventory = load_inventory(args.manifest, args.payload)
            return 0 if inventory is not None else 3
        if args.verb == "begin":
            intent = begin(host, args.manifest, args.payload)
            print("replace: begin -- " + ("no replacement declared" if intent is None else
                  "intent persisted, previous engine admission closed (%s -> %s)"
                  % (intent["previous_version"], intent["replacement_version"])))
            return 0
        if args.verb == "retire":
            retire(host, args.manifest, args.payload, args.delivered)
            return 0
        if args.verb == "config-install":
            target = config_install(host, args.pack, args.manifest)
            print("replace: kit config-install done (%s)" % target if target else
                  "replace: no session_configuration declared; kit config-install skipped")
            return 0
        if args.verb == "activation-keys":
            result = activation_keys(host, args.pack, args.manifest)
            if result is None:
                print("replace: no activation authority declared; activation keys skipped")
                return 0
            for name, rel, created, ledger in result:
                print("replace: %s key %s (%s)" % (name, "created" if created else "kept", rel))
                if ledger:
                    print("ledger: " + rel)
            return 0
        revocation_module(host).verify_attestation(host.root, args.pack)
        print("replace: absence attestation verified")
        return 0
    except (ReplaceError, OSError, ValueError, KeyError, TypeError) as error:
        print("replace: REFUSED -- %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
