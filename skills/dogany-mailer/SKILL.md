---
name: dogany-mailer
description: Send email on the user's behalf via their connected mail account. Triggers (user language + English) - "메일로 보내줘", "이메일로 보내", "메일 보내줘", "이거 메일로", "email me X", "send an email", "email this to <person>", "send <someone> a mail". Composes subject + body, auto-CCs the owner, and requires a pre-send confirm. If email is not connected, tells the user to connect it (points to onboarding), does not error out.
---

# dogany-mailer -- send email

Send mail via user's connected Google account (gws gmail) using the
service.mailer facade. Transport is gws -- no SMTP, no app password. One
OAuth login covers calendar + tasks + email (same Google onboarding). NEVER
hardcode any address. External action -> confirm before send (RULES).

## when to use
- user says: send X by mail / email this / "메일로 보내줘" / "email me the summary"
- any request to deliver text (summary, note, report) to an email address

## flow (strict order)
1. compose: figure out `to`, `subject`, `body` from the request.
   - `to` missing -> ask user for recipient (do NOT guess).
   - draft a clear subject + plain-text body.
2. CONFIRM (mandatory, external action): show user the `to`, `subject`, and body
   preview. Ask "send it?" and wait for yes. No send without explicit yes.
3. send: call the mailer service (below).
4. report result: on ok, tell user "sent to <to> (cc <cc>)". Owner is auto-CC'd
   by the service -- mention it. On failure, relay the result `message`.

## how to send (python, via service facade)
```python
import sys, os
# repo root = <repo>/service/.. ; service.mailer is the stable facade.
sys.path.insert(0, os.path.join(os.environ["PROJECT_ROOT"]))  # if service on path
from service import mailer

res = mailer.send(to="a@b.com", subject="Weekly summary", body="Hi,\n...\n")
# res: {"ok": True, "to": [...], "cc": [...], "subject": ...}
#  or  {"ok": False, "connected": False, "message": "Email is not connected..."}
```
If `service` is not importable by cwd, add the repo root that holds `service/` to
`sys.path` first (the dir containing the `service/` package).

## not connected (graceful)
- `res["connected"] is False` (or `is_configured()` False) -> DO NOT retry, DO NOT
  error. Tell user: email isn't connected yet; ask the agent to "connect Google"
  (same login as calendar sync). Point to the Google onboarding flow.

## auto-CC
- service always CCs the owner (EMAIL_CC from config) unless the owner is already
  in to/cc. Do not add it yourself.

## bounds
- external action: always confirm before send (step 2). no silent send.
- never hardcode an address.
- attachments: pass `attachments=[<abs path>]` only for files the user asked to
  attach.
