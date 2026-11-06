---
schema: dogany.work-class/1
name: framework.summarize
covers: "Condense supplied material into a shorter faithful account; no edits."
tools: [read-inputs, write-run-artifacts]
place: [claude-code, codex]
routes:
  simple: [sonnet@claude-code, opus@codex]
  standard: [sonnet@claude-code, opus@codex]
  hard: [opus@claude-code, opus@codex]
evidence: "Local runtime bindings; candidate availability is checked at execution."
qualification: provisional-class-binding
---

# framework.summarize

Condense supplied material into a shorter faithful account; no edits.

| Difficulty | Ordered candidates |
|---|---|
| simple | `sonnet@claude-code, opus@codex` |
| standard | `sonnet@claude-code, opus@codex` |
| hard | `opus@claude-code, opus@codex` |
