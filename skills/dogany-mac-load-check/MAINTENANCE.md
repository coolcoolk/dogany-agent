# Admin maintenance from chat

Diagnosis and process ownership checks remain read-only. For owner-approved
fseventsd maintenance on the representative instance, use
`bash <ROOT>/.claude/skills/dogany-mac-load-check/loadcheck.sh maintain restart-fseventsd`.
Run from the instance directory or set `CLAUDE_PROJECT_DIR=<ROOT>`.
Other agents continue to escalate machine-wide work to the representative.

The helper tries the exact passwordless whitelist first, then the native macOS
administrator dialog, and reports failure only after those routes fail.
The password goes directly to macOS on the user's screen; never ask for it
in chat, read it, store it, or log it. Never send terminal or SSH instructions
for this maintenance. Cancellation or an unavailable dialog means the action
is unresolved; report that in chat. A successful child exit produces
`status=completed`. See the Mac optimize maintenance guide for other approved
administrator commands. Linux is explicitly unsupported (exit 3).
