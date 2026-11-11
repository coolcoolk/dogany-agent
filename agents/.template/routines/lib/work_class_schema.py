"""DGN-2294 step 1: frozen v1 schemas for Job, WorkClass and Return.

Pure validation, no I/O. The routing contract (docs/design/DGN-2294-work-classes.md):
a caller declares a work CLASS and a DIFFICULTY, never a model, provider or
place. Routing is data in work-class files (routines/work-classes/*.md plus
kit overrides under config/packs/<kit>/work-classes/), resolved by
lib/work_class_resolver.py.

Versioned identifiers -- a new incompatible shape is a new number, never an
in-place change:
  dogany.job/1                 caller -> framework request
  dogany.work-class/1          one class definition or kit override file
  dogany.work-class-guard/1    framework authority ceiling (_guard.md)
  dogany.work-class-receipt/1  resolver output (route + provenance)
  dogany.return/1              executor -> checker result
"""
from __future__ import annotations

JOB_SCHEMA = "dogany.job/1"
CLASS_SCHEMA = "dogany.work-class/1"
GUARD_SCHEMA = "dogany.work-class-guard/1"
RECEIPT_SCHEMA = "dogany.work-class-receipt/1"
RETURN_SCHEMA = "dogany.return/1"

DIFFICULTIES = ("simple", "standard", "hard")

# Job lifecycle (design "Smallest core"). Attempts stay subordinate to one job.
LIFECYCLE = ("submitted", "preparing", "ready", "queued", "reserved", "running",
             "returned", "checking", "accepted", "failed", "incomplete", "canceled")
TERMINAL = ("accepted", "failed", "incomplete", "canceled")

# Public Job fields. No repository, ticket, bottleneck or model is required.
JOB_REQUIRED = ("schema", "idempotency_key", "goal", "immutable_inputs",
                "authorization", "budgets", "adapter", "checker", "return_sink")
JOB_OPTIONAL = ("class", "difficulty", "skill", "compute", "data",
                "registered_command")
# A caller never chooses the model/provider/place: these keys are refused,
# not ignored, so a stale caller cannot believe it pinned a route.
JOB_FORBIDDEN = ("model", "provider", "runtime", "place", "effort", "tier",
                 "axis", "lane", "route", "candidates")

# Kinds of fields a work-class file may carry.
CLASS_DEFINE_KEYS = ("schema", "name", "extends", "covers", "tools", "place",
                     "routes", "evidence", "required", "forbidden_compute",
                     "qualification")
CLASS_OVERRIDE_KEYS = ("schema", "overrides", "covers", "tools_add",
                       "tools_remove", "place", "routes", "evidence",
                       "required", "forbidden_compute", "qualification")
CLASS_SCALARS = ("covers", "evidence", "qualification")
CLASS_LISTS = ("place", "required", "forbidden_compute")

RETURN_REQUIRED = ("schema", "idempotency_key", "attempt", "execution",
                   "artifacts", "checker", "outcome")
RETURN_OUTCOMES = ("accepted", "failed", "incomplete", "canceled")


class SchemaError(ValueError):
    """A document does not satisfy its frozen schema."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _need_str(doc, key, where):
    val = doc.get(key)
    if not isinstance(val, str) or not val.strip():
        raise SchemaError("schema", f"{where}.{key} must be a non-empty string")
    return val


def validate_job(job):
    """Validate a dogany.job/1 mapping. Returns the job unchanged.

    class/difficulty are optional HERE because an active skill may supply
    them; the resolver rejects a job that ends up with neither.
    """
    if not isinstance(job, dict):
        raise SchemaError("schema", "job must be a mapping")
    if job.get("schema") != JOB_SCHEMA:
        raise SchemaError("schema", f"job.schema must be {JOB_SCHEMA!r}")
    bad = sorted(k for k in job if k in JOB_FORBIDDEN)
    if bad:
        raise SchemaError("caller-route",
                          f"job declares {', '.join(bad)}; a caller names a class "
                          "and difficulty, never a model/provider/place")
    unknown = sorted(k for k in job if k not in JOB_REQUIRED + JOB_OPTIONAL)
    if unknown:
        raise SchemaError("schema", f"job has unknown fields: {', '.join(unknown)}")
    for key in JOB_REQUIRED:
        if key not in job:
            raise SchemaError("schema", f"job.{key} is required")
    _need_str(job, "idempotency_key", "job")
    _need_str(job, "goal", "job")
    _need_str(job, "adapter", "job")
    _need_str(job, "return_sink", "job")
    if not isinstance(job["immutable_inputs"], list):
        raise SchemaError("schema", "job.immutable_inputs must be a list")
    for i, item in enumerate(job["immutable_inputs"]):
        if not isinstance(item, dict) or not item.get("ref") or not item.get("sha256"):
            raise SchemaError("schema",
                              f"job.immutable_inputs[{i}] needs ref and sha256")
    auth = job["authorization"]
    if not isinstance(auth, dict) or not isinstance(auth.get("tools"), list):
        raise SchemaError("schema", "job.authorization.tools must be a list")
    if not isinstance(job["budgets"], dict):
        raise SchemaError("schema", "job.budgets must be a mapping")
    checker = job["checker"]
    if not isinstance(checker, dict) or not checker.get("name") or not checker.get("version"):
        raise SchemaError("schema", "job.checker needs name and version")
    if "class" in job:
        _need_str(job, "class", "job")
    if "difficulty" in job and job["difficulty"] not in DIFFICULTIES:
        raise SchemaError("schema",
                          f"job.difficulty must be one of {', '.join(DIFFICULTIES)}")
    for key in ("compute", "data"):
        if key in job and not isinstance(job[key], dict):
            raise SchemaError("schema", f"job.{key} must be a mapping")
    if "registered_command" in job:
        _need_str(job, "registered_command", "job")
    return job


def validate_return(ret):
    """Validate a dogany.return/1 mapping. Returns it unchanged.

    Exit code zero is an execution fact, not acceptance: outcome 'accepted'
    requires the predeclared checker's verdict 'pass' and at least one
    hashed artifact.
    """
    if not isinstance(ret, dict):
        raise SchemaError("schema", "return must be a mapping")
    if ret.get("schema") != RETURN_SCHEMA:
        raise SchemaError("schema", f"return.schema must be {RETURN_SCHEMA!r}")
    for key in RETURN_REQUIRED:
        if key not in ret:
            raise SchemaError("schema", f"return.{key} is required")
    _need_str(ret, "idempotency_key", "return")
    if not isinstance(ret["attempt"], int) or isinstance(ret["attempt"], bool) or ret["attempt"] < 1:
        raise SchemaError("schema", "return.attempt must be an integer >= 1")
    execution = ret["execution"]
    if not isinstance(execution, dict) or "exit_code" not in execution:
        raise SchemaError("schema", "return.execution.exit_code is required")
    if not isinstance(ret["artifacts"], list):
        raise SchemaError("schema", "return.artifacts must be a list")
    for i, art in enumerate(ret["artifacts"]):
        if not isinstance(art, dict) or not art.get("path") or not art.get("sha256"):
            raise SchemaError("schema", f"return.artifacts[{i}] needs path and sha256")
    checker = ret["checker"]
    if not isinstance(checker, dict) or not checker.get("name") or not checker.get("version"):
        raise SchemaError("schema", "return.checker needs name and version")
    outcome = ret["outcome"]
    if outcome not in RETURN_OUTCOMES:
        raise SchemaError("schema",
                          f"return.outcome must be one of {', '.join(RETURN_OUTCOMES)}")
    if outcome == "accepted":
        if checker.get("verdict") != "pass":
            raise SchemaError("unchecked-accept",
                              "accepted requires checker.verdict 'pass'; exit code "
                              "zero alone is not acceptance")
        if not ret["artifacts"]:
            raise SchemaError("unchecked-accept", "accepted requires a hashed artifact")
    return ret
