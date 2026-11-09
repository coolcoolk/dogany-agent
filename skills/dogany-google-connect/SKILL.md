---
name: dogany-google-connect
display_name: 구글 캘린더 연결
scope: framework
layer: shipped
lint: no-user-data
description: >-
  Connect Google calendar and let the owner choose an event calendar,
  with lifekit absent or off. Fires on "구글 연결해줘", "구글 캘린더 연결",
  "캘린더 연결", "connect Google", "connect my calendar", or "connect calendar".
  The agent handles setup; the owner opens one consent link. Requests calendar
  only; mail sending has its own consent in the mailer skill.
---

# Connect Google calendar

This is a framework connection. Do not install or activate lifekit, edit kit
configuration or create tasklists. Installed kit with `LIFEKIT=on`: hand off
to `dogany-lifekit-setup`; its calendar + tasks flow stays unchanged.

1. Work from the instance root with `PROJECT_ROOT` exported to its absolute
   path (needed for the existing bridge inbox relay). Export `AGENT_LANG` from
   `config/agent.conf` (`en` or `ko`, default `en`). Start:
   ```
   python3 -m service.google connect start --origin "<instance>:<chat>" --scopes calendar --json
   ```
   `<instance>` is the instance directory basename, `<chat>` this private chat's
   id. Send the returned `copy` lines and `url` as-is, once. A failed start:
   explain its class using `service.google.errors` owner copy and stop; offer
   a fresh link when the owner retries. Never send auth commands to the owner.
2. Wait for the matching `[google-connect-result] class=<class> sub=<sub>
   origin=<instance>:<chat>` session-inbox line. The existing bridge handles
   a pasted loopback address; never read, echo or process that address, and
   never ask the owner to run commands. On a local browser the callback relays
   automatically; the returned copy covers phone/remote browser recovery.
   Only `class=ok` for this origin advances. On failure explain the stable
   class; a later retry starts a fresh link. Do not infer success from a paste.
3. With success line's `sub`, get choices (read-only):
   ```
   python3 -m service.calendar.connect candidates --instance "<absolute-instance-root>" --account "<sub>" --locale "<AGENT_LANG>" --json
   ```
   `bound=true`: saved pick kept; send returned `copy`, stop. No question,
   creation or duplicate. Otherwise send ONE message: returned `copy`, then
   plain numbered list of `candidates[].label` in returned order, then exact
   `[[OPTIONS]]` marker as LAST line. Labels verbatim; no paraphrase, extra
   choices or auto-pick. Korean prompt:
   "구글 캘린더가 연결됐어요. 에이전트가 일정을 쓸 캘린더를 정해 주세요."
   Helper filters writable calendars, pins exact `Dogany` recommendation,
   ranks long lists by last 30 days' event counts. Keep each button's `id`
   and `kind` for mapping owner's tap; never use label as calendar id.
4. Wait for owner's tap. Never select on owner's behalf. `kind=more`: rerun
   candidates with `--all`, send same prompt + all returned labels + final
   `[[OPTIONS]]`, wait again. `kind=create` or `kind=existing`: bind that pick:
   ```
   python3 -m service.calendar.connect bind --instance "<absolute-instance-root>" --account "<sub>" --locale "<AGENT_LANG>" --create --json
   python3 -m service.calendar.connect bind --instance "<absolute-instance-root>" --account "<sub>" --locale "<AGENT_LANG>" --calendar-id "<chosen-id>" --json
   ```
   Run only matching command; pass ids as safely quoted arguments. Helper
   revalidates write access, saves `google.calendar_id` + `summary` + account
   in framework `config/calendar.json` (0600), preserving other settings.
   Success: send returned `copy` as ONE line, verbatim. Korean create result:
   "Dogany 캘린더를 만들었어요. 앞으로 일정은 여기에 써요."
   Existing result: "'<calendar name>' 캘린더에 연결했어요. 앞으로 일정은 여기에 써요."
   Any helper failure: setup incomplete; explain stable class, retry failed
   step after fixing cause. Stale/inaccessible pick: fetch candidates again,
   wait for new tap. No fresh consent unless consent missing.

Scopes are `calendar` only (plus the core's identity scopes). Do not request
Tasks or `gmail.send`; the mailer owns mail consent. Existing Tasks verbs are
available only when a separate feature explicitly asks for them.
