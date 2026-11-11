---
schema: dogany.work-class/1
name: framework.review
covers: "Independently check supplied work against its stated requirements; report findings, no edits."
tools: [read-inputs, write-run-artifacts]
place: [claude-code, codex]
routes:
  simple: [sonnet@claude-code, opus@codex]
  standard: [opus@claude-code, opus@codex]
  hard: [opus@claude-code, opus@codex]
evidence: "Local runtime bindings; candidate availability is checked at execution."
qualification: provisional-class-binding
---

# framework.review

Independently check supplied work against its stated requirements; report findings, no edits.

| Difficulty | Ordered candidates |
|---|---|
| simple | `sonnet@claude-code, opus@codex` |
| standard | `opus@claude-code, opus@codex` |
| hard | `opus@claude-code, opus@codex` |
