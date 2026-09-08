# MEMORY (cold store)

<!-- Engine-owned. The agent NEVER writes under memories/ -- the engine lands facts
     in inbox.md (nightly consolidate) and routes them weekly (classify-inbox).
     Durable user facts -> identity/hot.custom.owner.md;
     agent identity -> identity/hot.custom.agent.md. -->
