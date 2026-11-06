Synthetic fixtures for the Why net suites (v2.7 T1). No real transcripts
(spec section 10). Timestamps are 2026-10-07/08 KST.
- transcript-floor.jsonl: owner, assistant, injected, Stop-hook, recall, recap,
  meta, tool_result, task-notification and tap turns (test-why-net-floor.py).
  The suite submits every user line through `why-net ups` first, so only the
  real owner turns land in the owner-turn ledger.
- transcript-e4-assistant-only.jsonl: the quote exists only in an assistant turn (E4).
- transcript-e14-label-absent.jsonl: the reply after an inline ask omits its labels (E14).
The grill regression suite (test-why-net-grill.py) builds its transcripts
in-test, turn by turn, the way the live hook sees them.
Every fixture entry carries uuid + parentUuid (each links to the entry
before it), the main chain Claude Code writes: the resolver accepts only
on-chain owner turns (grill-2 fixes 1-3).  test-why-net-grill2.py replays
the grill round 2 attacks on the same in-test harness.
