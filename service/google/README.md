# Google connection core (DGN-1854 W1)

The public API is `get_access_token(scopes, account=None)`, `status(account=None)`
and `call(method, url, params=None, body=None, etag=None, scopes=...)`.
Only the explicit `token` CLI command returns an access token. Auth failures carry
stable `.cls` and `.key`; HTTP failures carry `.code`, `.http_code`, `.reason`
and raw `.body` for internal compatibility. Never show `.body` to an owner.
`errors.render` is the owner-copy entry point, with English and Korean catalogs.

The store is `~/.dogany/google`, with one flock for transactions, 0700
directories and atomic 0600 JSON files. Imports never overwrite account records
or write to the gws directory. Only legacy import needs `cryptography`.
The installed-app client in `client.json` belongs to project `dogany-agent`;
the desktop client is distributed intentionally, as required by the locked
spec. Its client ID matches the existing owner's gws configuration in the
read-only build check. Refresh tokens are never bundled.

The CLI implements status, connect start/finish, token, import-gws,
set-default and disconnect. The listener inherits an already-bound loopback
socket. Both completion paths durably claim the same pending record before
redeeming a code. Completed and expired flow records remain available for the
bridge's 24-hour blocked-screen heuristic. No callback values appear in logs,
static browser pages or owner error results.

The module is shipped through the existing root `service/` delivery in mint
and update. There was no template `service/` copy to synchronize.

## Integration boundaries

W2, W3, W5 and the kit transport changes are separate build slices. No bridge,
mailer, mirror, skill, release version or live auth store was changed here.

For host-browser completion, the caller must provide `PROJECT_ROOT` matching
the instance component of `--origin <instance>:<chat>`; the module writes an
atomic `[google-connect-result]` message to that instance's existing
`.telegram_bot/session-inbox`. Finish JSON also includes `sub` on success.
The bridge must restrict completion to the owner and consume orphan URL pastes.

The bridge can claim the once-per-chat prompt with the internal
`_expired-prompt --origin` command and record its answer with
`_blocked-screen --origin --answer block|missed`. The latter returns
`operator_alert` for the notification consumer and records a local diagnostic;
this core does not itself send operator notifications. Integrating that
notification and the prompt controls remains W2 work. A successful refresh
clears the account pause marker; health/drain behavior belongs to W5/W4.

## Verification record

`selftest.py` uses a temporary HOME and fake loopback token/API endpoints.
It covers process concurrency, consent/import races, PKCE, scope union,
account mismatch, default stability, disk failures, legacy encryption rotation,
permissions, copy redaction, pause recovery, cap state, and REST error parity.

The attempted command was:

```
bash git-hooks/run-tests.sh --gate python3 service/google/selftest.py
```

The machine-wide gate returned **77** because the sandbox could not create
`~/.dogany/cron-queue/worker-test/slot-1`. No selftest ran. Per dispatch rules,
no further tests or lanes were attempted. The dispatcher must run the selftest
and the affected lane before integration. Live consent, console production
status, the day-8 refresh rehearsal and the existing-instance soak remain release gates.
