# Calendar connection seam

Google is the only Dogany-provided two-way service. The existing
`CALENDAR_BACKEND=auto|google` configuration continues to work. The legacy
`apple` choice remains unavailable (`not_built`) for compatibility; an Apple
service is no longer planned.

Put settings in `<instance>/config/calendar.json` (JSON, never shell code).
Keep the file private (0600) when it contains app passwords or private feeds.
For example:

```json
{
  "write_target": "google",
  "backends": {
    "caldav": {
      "server": "https://calendar.example.test/dav/calendars/user/default/",
      "account": "user@example.test",
      "app_password": "replace-locally"
    }
  },
  "subscriptions": [
    {"id": "holidays", "url": "https://example.test/holidays.ics"},
    {"id": "work", "url": "webcal://example.test/team.ics"}
  ]
}
```

Change only `write_target` to `caldav` to select that entry. Without an
installed driver this reports `backend_not_available`; no connection or write
is attempted and it never falls back to Google. A CalDAV client is **not
bundled**. Translating the kit's calendar/task verbs, recurrence, identity and
sync semantics is beyond a small stdlib-only client.

A user-installed backend needs no framework change: add
`"driver": "my_calendar_connector:open_connector"` to its entry. The installed
Python module must be importable by the same interpreter running the service.
The factory accepts the entry dict (including server, account and credentials)
and returns an object with `call(*gws_args, body=None)`, `delete(*gws_args)`,
`http(method, url, params=None, body=None, etag=None)` and `health()`.
It translates the frozen verbs in `verbs.py` into its provider's operations.
The factory and `health()` perform local checks only. Imports execute code:
configure drivers you trust, as you would an installed Python package.

Its `capabilities` dict must use exactly the frozen keys:
`calendar_create_sources`, `event_color`, `tasks_kind`, `change_feed`,
`offline_write`, `attendees_write`, `window`. Values may differ by backend;
the existing capability call sites do not change. Raise `GwsError` with
`http_code` and `reason`, or `CalendarPaused(cls)` for a pause. The seam wraps
other exceptions and preserves the kit's `error_class` binding. The configured
entry name and account form its stable account key, e.g.
`caldav:user@example.test`; server/credentials are never included in status.
Changing a server for the same name/account is the user's responsibility;
use a distinct entry name for a different calendar identity.

Only one `write_target` string is accepted. Several backend entries may be
saved, but only the selected one is opened. Explicit `CALENDAR_BACKEND` values
must agree with the JSON selection (`auto` defers to JSON). Conflicting targets
fail with `dual_target`. An existing mirror target stamp pins the backend and
account; changing account fails with `target_mismatch`. The seam reads the
stamp without writing it. Re-engraving and kit transport integration remain
kit responsibilities.

`service.calendar.sources(instance)` returns a tuple of read-only subscriptions,
even if the write backend is unavailable. `open(instance).subscriptions` exposes
the same sources. Each source has `id`, `backend="ics"`, `read_only=True`, and
`read()` returning original UTF-8 iCalendar text. Reads use GET with a 30-second
timeout and an 8 MiB limit; `webcal` means HTTPS. Sources accept any HTTP(S)
provider, including private URL feeds. The consuming kit parses and displays
the iCalendar text, preserving recurrence and timezone data. The seam does not
schedule polling, expand recurrences, merge feeds into the write target, or
create an ICS write path; `call/delete/http` on a subscription raise `read_only`.

Inspect configuration without credentials or network requests:

```sh
python3 -m service.calendar resolve --instance /path/to/instance --json
python3 -m service.calendar status --instance /path/to/instance --json
```

`resolve` validates configuration without importing user drivers. `status` also
constructs the selected driver and reads its local health. Missing/broken
drivers produce `backend_not_available`. ICS URLs, passwords, server addresses
and item bodies are never emitted by either command.

The framework `dogany-google-connect` skill runs calendar-only consent, then
`python3 -m service.calendar.connect candidates --instance /path/to/instance --account <sub> --json`
after the matching success relay. This read-only step returns localized button
labels and ids for writable calendars, plus a create choice unless an exact
`Dogany` calendar already exists. More than six writable calendars: show five
ranked by event counts over the last 30 days (calendarList order breaks ties),
with `Dogany` pinned first when present, and a final more choice. `candidates
--all` expands the list without ranking. The create choice precedes the five
calendar choices when no `Dogany` exists.

After the owner chooses, run `bind --create` or `bind --calendar-id <id>` with
the same instance and account arguments. Only bind writes. Its atomic, 0600
framework binding is stored in `config/calendar.json` under
`{"google":{"calendar_id":"...","summary":"...","account_key":"google:<sub>"}}`,
preserving write-target, driver and subscription settings. A saved choice
returns `bound=true` without asking again or creating another calendar.
No tasklists or kit routines are created. Framework health requires only calendar
scope. For an installed mirror with `LIFEKIT=on`, instance health requires
calendar + tasks (dec-663): the current kit still provisions a Tasks checklist.
The connector keeps this requirement per instance; an absent/off kit never
adds Tasks to framework consent. Tasks verbs remain available to explicit
callers with their own grant.
