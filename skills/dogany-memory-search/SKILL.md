---
name: dogany-memory-search
display_name: 기억 검색
description: Use to recall past facts, records, preferences, or context about the user. Fires on "what did I say before", "what did I tell you about X", recall of the user's workout/diet/investment/budget records, personal details, schedule, relationships, or past decisions - any long-term memory lookup. Search with this BEFORE answering such a question. Semantic cross-lingual search finds by meaning even when keywords do not overlap (also catches non-English utterances), and the search itself costs zero tokens. MUST also search before claiming any user data/preference/record/guide is absent or unknown -- fires whenever the agent is about to say "기록에 없다", "안 정해져 있다", "저장돼 있지 않다", "모른다 -- 알려주세요", "다시 알려주시면", "not recorded", "no saved value", "I don't have that", or any equivalent absence claim about user data. Only after a 0-hit search may the agent state the fact is absent. The same gate covers DOMAIN-KNOWLEDGE absence claims -- fires before saying "그 지식은 없다", "창고에 없다", "자료가 없다", "no domain knowledge on that", or any claim that the agent lacks knowledge in its own domain: when config/agent.conf sets KNOWLEDGE_WAREHOUSE, also check the knowledge/<name>/ warehouse before any such claim.
---

# dogany-memory-search — long-term memory recall

Persistent facts about the user stored in `memories/*.md` as § atomic notes.
Indexed by `memory-engine/memory.py` using bge-m3 embeddings (local Ollama) + SQLite FTS5 (trigram).
Hybrid search (FTS + vector, RRF fusion) -> finds by meaning even without keyword match.

## when to use
- need to reference user's past utterances, records, preferences, profile, relations, schedule, decisions
- uncertain fact -> search before guessing (recall = 0 tokens, no cost concern)
- "do I already know this about the user?" -> search first

## work-item exclusion
memory stores durable facts only -- work-items (backlog, parked ideas, tasks, to-dos, pending decisions) are NOT stored here.
work-items live in the agent's worklog/ ticket surface; route them to a worklog ticket instead.
write path refuses work-item shapes (DGN-446 gate) -- do not attempt to write tasks or decisions to memory.

## absence claim gate
before saying user data is missing, unknown, or not recorded -> run this search FIRST.
only after 0-hit search may you say "not in memory (searched)".
never ask user to re-provide a fact without running the search.
applies to cron/proactive outputs (briefings, daily summaries) too -- recall hook does not cover those paths; the skill call is the only gate.
domain-knowledge claims: if config/agent.conf sets KNOWLEDGE_WAREHOUSE=<name>
AND knowledge/<name>/ exists, a 0-hit memory search alone is NOT enough --
also check knowledge/<name>/ (registry.yaml -> items/ -> e/) before stating
the knowledge is absent. Key set but directory missing -> say the warehouse
is unavailable (do not retry-loop). No KNOWLEDGE_WAREHOUSE key -> memory
search alone gates.

## usage
```bash
cd memory-engine
/usr/bin/python3 memory.py search "search_query" --k 5
```
- search_query = core meaning of user's question in natural language. no need to extract only nouns; semantic match works across paraphrase.
- use `source_file › section` and original text from results as basis for answer.
- empty/irrelevant results -> honestly say "not in memory", do not guess.
- need more results -> increase `--k`. need JSON -> add `--json`.

## memory update (new fact learned)
user says something worth remembering permanently -> use compressed write. markdown = source of truth; compression + meta-tagging + index update all in one step.

### recommended — compressed write
pass raw text (user utterance etc.) -> cheap model (Haiku) extracts only persistent facts, compresses to single-line atomic items, auto-attaches `(YYYY-MM-DD, source)` meta, writes to file, updates index.
```bash
cd memory-engine
echo "user utterance / context" | /usr/bin/python3 memory.py write --source "chat"
```
- target is ALWAYS `inbox.md` (staging). the engine routes it later: weekly
  classify-inbox moves items into topic files under `memories/`. do NOT write
  topic files directly (RULES: memories/ is engine-owned) -- durable knowledge
  with a clear home goes to USER.md / AGENT.md / the relevant SKILL.md instead.
- add `--dry-run` first to preview what gets compressed/written without touching files. ambiguous -> dry-run, show user, then write.
- casual/transient content -> model discards. nothing worth keeping -> no write.

## operational notes
- index (`state.db`) = cache, regenerable anytime from markdown via `index`. do not store data only in db.
- search quality -> `python memory.py stats` for miss rate. frequent high miss rate -> report to user (index repair / model check signal).
- changed refinement rules (noise filters) -> incremental hash skips them; do 1 full reindex: rm state.db then python memory.py index.
