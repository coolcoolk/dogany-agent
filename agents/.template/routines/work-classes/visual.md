---
schema: dogany.work-class/1
name: framework.visual
covers: "Produce or revise a visual artifact with an image self-check."
tools: [read-inputs, render-image, write-draft, write-run-artifacts]
place: [claude-code, codex]
routes:
  simple: [sonnet@claude-code, opus@codex]
  standard: [opus@claude-code, opus@codex]
  hard: [opus@claude-code, opus@codex]
evidence: "Local runtime bindings; candidate availability is checked at execution."
qualification: provisional-class-binding
---

# framework.visual

Produce or revise a visual artifact with an image self-check.

| Difficulty | Ordered candidates |
|---|---|
| simple | `sonnet@claude-code, opus@codex` |
| standard | `opus@claude-code, opus@codex` |
| hard | `opus@claude-code, opus@codex` |
