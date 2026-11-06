# Admin maintenance from chat

For owner-approved administrator maintenance, call
`bash <ROOT>/.claude/skills/dogany-mac-optimize/gate.sh maintain restart-fseventsd`
from the instance directory (or set `CLAUDE_PROJECT_DIR=<ROOT>`).
This is an explicit machine maintenance action; diagnosis remains read-only.
Keep the existing representative, ownership, and owner approval gates.

The framework helper first tries the exact installed whitelist without a
terminal prompt. If unavailable, it opens the native macOS administrator
password dialog on the Mac's screen. The user types directly into macOS;
never ask for, read, store, or log their password. Only report a failure after
both routes fail, including cancellation or an unavailable GUI session.
Do not send terminal or SSH instructions. Report success only for
`status=completed`; otherwise describe the unresolved action in chat.

For an approved command outside the whitelist, use
`python3 <ROOT>/routines/mac-admin.py -- /absolute/command argument ...`.
Pass separate arguments, with shell quoting as needed, rather than a shell
command string. Only the fixed `/usr/bin/killall fseventsd` command receives
passwordless sudo; other commands always use the native dialog.
The helper never captures child command output or authentication messages.
Linux returns `status=not-supported` and exit 3 without attempting elevation.
