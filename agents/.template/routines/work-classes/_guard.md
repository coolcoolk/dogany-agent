---
schema: dogany.work-class-guard/1
# Authority ceiling for EVERY layer (framework, kit, skill). A class file,
# kit override or skill may only name tools, places and models listed here.
# baseline-edit, publish, notify, merge, push-protected-ref and
# owner-profile-edit are refused by the resolver even if added below.
tools: [read-inputs, read-baseline, web-read, write-run-artifacts, write-draft, write-branch, run-declared-command, render-image]
places: [claude-api, codex, claude-code, script]
models:
  claude-api: [sonnet, opus]
  codex: [opus]
  claude-code: [sonnet, opus]
  script: [exec]
predicates: [registered-command]
---

# Work-class guard

Capability ceiling for framework and explicitly installed kit routing data.
The framework classes select local runtimes. A kit may supply other ordered
candidates within this ceiling; the resolver itself never launches a runtime
or grants publication, merge, protected-state, or notification authority.
