# dogany-agent

Part of **Dogany** — 에이전트를 빚어내는 도가니.

Clean-slate rebuild of the Dogany agent: a project/task-managed multi-agent
organization you run as the CEO of your life. You set the Why; the agents propose
and execute the How/What, and your taste + daily progress become legible over time.

## Design direction (WIP)
- Two-substrate memory: a structured lane (metrics/state/entities; migration-ready,
  LITE = lightweight store, PRO = SQL + GUI console) + a prose memory vault
  (consolidation, agent/shared split, safe non-destructive upkeep).
- LITE (memory-managed) -> PRO (SQL + operations console GUI = the paid tier).
- Hierarchy: user > main agent > domain agents > sub-agents.

Status: WIP. Design docs land in design/ as they solidify.
