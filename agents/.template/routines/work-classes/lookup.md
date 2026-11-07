---
schema: dogany.work-class/1
name: framework.lookup
covers: "Find and report facts from supplied or readable sources; no edits."
tools: [read-inputs, web-read, write-run-artifacts]
place: [claude-code, codex]
routes:
  simple: [sonnet@claude-code, opus@codex]
  standard: [sonnet@claude-code, opus@codex]
  hard: [opus@claude-code, opus@codex]
evidence: "Local runtime bindings; candidate availability is checked at execution."
qualification: provisional-class-binding
---

# framework.lookup

Find and report facts from supplied or readable sources; no edits.

| Difficulty | Ordered candidates |
|---|---|
| simple | `sonnet@claude-code, opus@codex` |
| standard | `sonnet@claude-code, opus@codex` |
| hard | `opus@claude-code, opus@codex` |
