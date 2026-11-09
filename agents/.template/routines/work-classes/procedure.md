---
schema: dogany.work-class/1
name: framework.procedure
covers: "Run a specified procedure: an exact registered command (X), otherwise bounded model orchestration."
tools: [read-inputs, run-declared-command, write-run-artifacts]
place: [claude-code, codex]
routes:
  simple: [sonnet@claude-code, opus@codex]
  standard: [sonnet@claude-code, opus@codex]
  hard: [opus@claude-code, opus@codex]
evidence: "Local runtime bindings; candidate availability is checked at execution."
qualification: provisional-class-binding
---

# framework.procedure

Run a specified procedure: an exact registered command (X), otherwise bounded model orchestration.

| Difficulty | Ordered candidates |
|---|---|
| simple | `sonnet@claude-code, opus@codex` |
| standard | `sonnet@claude-code, opus@codex` |
| hard | `opus@claude-code, opus@codex` |
