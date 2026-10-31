# Pack flow taps (DGN-2036)

A pack may declare one flow handler in the instance's `config/agent.conf`:

```sh
FLOW_TAP_COMMAND='["service/health/flow", "tap"]'
```

The value is a nonempty JSON array of strings, optionally quoted as above.
It is read on every tap, without environment interpolation or shell evaluation.
The executable is absolute or relative to the instance root; it is never
looked up on PATH. Use an absolute interpreter path for non-executable scripts.
This is a generic pack hook; the bridge has no health-specific routing.

After the normal access check, the bridge answers each `flow|` callback and
runs the declared argv with the entire callback appended as ONE final argument,
with the instance root as cwd. For example:

```text
service/health/flow tap 'flow|<sid>|<ver>|<event>|<arg>'
```

The callback is opaque to the bridge. The pack must validate session, revision,
event and argument, reject stale input, and own any message edits or responses.
Exit 0 means handled: the bridge preserves the keyboard and starts no model
turn. Stdout and stderr are not chat output. Execution runs off the event loop
and has a 15-second timeout.

Absent or invalid configuration, launch failure, nonzero exit or timeout emits
one localized notice and passes the original callback through the existing
queued callback text path. Failure to send the notice does not prevent fallback.
The original callback is the model text input, with the existing chat/thread/
message metadata. The original keyboard stays intact.
A handler should validate before mutating and make repeated taps idempotent:
a timeout or nonzero exit may follow a partial operation, and fallback must not
duplicate it. Other callback prefixes retain their existing routing.
