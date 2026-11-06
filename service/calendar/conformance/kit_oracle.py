"""DGN-1854 W5 -- the kit's gws result parsing, as the parity oracle.

`kit_gws` and `kit_gws_delete` are the bodies of adapter.gws() and
adapter.gws_delete() (dogany-lifekit/payload/mirror/adapter.py:827-866) with
subprocess.run replaced by a recorded (stdout, stderr, returncode). Only the
parsing is copied; the logic is unchanged line for line. They turn a recorded
gws CLI run into what the kit saw: a dict / True, or a raised GwsError with
(http_code, reason, message).

English/ASCII only.
"""

import json


class OracleGwsError(RuntimeError):
    def __init__(self, msg, code=0, reason=""):
        super().__init__(msg)
        self.http_code = code
        self.reason = reason


def kit_gws(stdout, stderr, returncode, args=()):
    stderr_clean = "\n".join(
        l for l in stderr.splitlines() if "keyring" not in l.lower())
    if not stdout.strip():
        raise OracleGwsError("gws no output: %s\nArgs: %s" % (stderr_clean, args))
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise OracleGwsError("gws invalid JSON: %s" % stdout[:200])
    if "error" in data and isinstance(data["error"], dict):
        err = data["error"]
        raise OracleGwsError("API error %s %s: %s" % (
            err.get("code", 0), err.get("reason", ""), err.get("message", "")),
            code=err.get("code", 0), reason=err.get("reason", ""))
    return data


def kit_gws_delete(stdout, stderr, returncode):
    if returncode == 0:
        return True
    try:
        data = json.loads(stdout)
        err = data.get("error", {})
        raise OracleGwsError("API error %s: %s" % (err.get("code", 0), err.get("message", "")),
                             code=err.get("code", 0), reason=err.get("reason", ""))
    except (json.JSONDecodeError, AttributeError):
        raise OracleGwsError("gws delete failed (exit %d): %s"
                             % (returncode, stderr[:200]))


def outcome(kind, stdout, stderr, returncode):
    """Normalized kit-visible outcome of one recorded run."""
    try:
        if kind == "delete":
            val = kit_gws_delete(stdout, stderr, returncode)
        else:
            val = kit_gws(stdout, stderr, returncode)
    except OracleGwsError as e:
        return {"raises": {"http_code": e.http_code, "reason": e.reason,
                           "message": str(e)}}
    return {"returns": val}
