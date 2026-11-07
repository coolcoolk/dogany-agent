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

A pack appends `|c` to a commit tap. The bridge acknowledges it immediately
and temporarily replaces its keyboard with one inert progress button. Selection
taps omit the marker and retain their keyboard. Taps are serialized per session;
an identical callback already in flight is counted once.

The callback is opaque to the bridge. The pack must validate session, revision,
event and argument, reject stale input, and own any message edits or responses.
Exit 0 means handled unless the first stdout line starts with `FLOW_REFUSED`.
The first stdout line is logged (at most 120 characters) but is never chat
output; stderr is discarded. Execution runs off the event loop
and has a 15-second timeout.

Absent or invalid configuration, launch failure, nonzero exit or timeout emits
one localized notice and passes the tapped button's visible label (never the
raw callback data) through the existing queued callback text path; a callback
that matches no button on the message gets the notice and no model turn.
Failure to send the notice does not prevent fallback. The callback metadata and
original keyboard accompany that model input.
A handler should validate before mutating and make repeated taps idempotent:
a timeout or nonzero exit may follow a partial operation, and fallback must not
duplicate it. Other callback prefixes retain their existing routing.
