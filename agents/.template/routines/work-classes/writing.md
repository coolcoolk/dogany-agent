---
schema: dogany.work-class/1
name: framework.writing
covers: "Draft prose for the owner to choose from; owner copy/choice gates stay independent."
tools: [read-inputs, write-draft, write-run-artifacts]
place: [claude-code, codex]
routes:
  simple: [sonnet@claude-code, opus@codex]
  standard: [opus@claude-code, opus@codex]
  hard: [opus@claude-code, opus@codex]
evidence: "Local runtime bindings; candidate availability is checked at execution."
qualification: provisional-class-binding
---

# framework.writing

Draft prose for the owner to choose from; owner copy/choice gates stay independent.

| Difficulty | Ordered candidates |
|---|---|
| simple | `sonnet@claude-code, opus@codex` |
| standard | `opus@claude-code, opus@codex` |
| hard | `opus@claude-code, opus@codex` |
