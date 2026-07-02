---
name: dogany-memory-search
description: Use to recall past facts, records, preferences, or context about the user. Fires on "what did I say before", "what did I tell you about X", recall of the user's workout/diet/investment/budget records, personal details, schedule, relationships, or past decisions - any long-term memory lookup. Search with this BEFORE answering such a question. Semantic cross-lingual search finds by meaning even when keywords do not overlap (also catches non-English utterances), and the search itself costs zero tokens.
---

# dogany-memory-search — long-term memory recall

Persistent facts about the user stored in `memories/*.md` as § atomic notes.
Indexed by `memory/memory.py` using bge-m3 embeddings (local Ollama) + SQLite FTS5 (trigram).
Hybrid search (FTS + vector, RRF fusion) -> finds by meaning even without keyword match.

## when to use
- need to reference user's past utterances, records, preferences, profile, relations, schedule, decisions
- uncertain fact -> search before guessing (recall = 0 tokens, no cost concern)
- "do I already know this about the user?" -> search first

## usage
```bash
cd memory
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
cd memory
echo "user utterance / context" | /usr/bin/python3 memory.py write --source "텔레그램 대화"
```
- default target: `inbox.md` (unsorted temp). if topic clear -> `--file`: identity->identity.md / work-rules->work-rules.md / routines->routines.md / infra->infra.md / user profile+health->about-user.md. section within file: `--section "header"`. (ambiguous -> inbox.md; nightly cleanup distributes to topic files)
- add `--dry-run` first to preview what gets compressed/written without touching files. ambiguous -> dry-run, show user, then write.
- casual/transient content -> model discards. nothing worth keeping -> no write.

### manual — direct edit
need exact wording -> add `§` atomic item directly to `memories/*.md`. each item must have `(YYYY-MM-DD, source/context)` meta. then update index:
```bash
cd memory
/usr/bin/python3 memory.py index
```
- only changed files re-embedded (incremental). low overhead.

## operational notes
- index (`state.db`) = cache, regenerable anytime from markdown via `index`. do not store data only in db.
- search quality -> `python memory.py stats` for miss rate. frequent high miss rate -> report to user (index repair / model check signal).
- changed refinement rules (noise filters) -> incremental hash skips them; do 1 full reindex: rm state.db then python memory.py index.
