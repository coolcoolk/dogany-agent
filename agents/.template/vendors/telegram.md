# telegram.md -- Telegram vendor contract (options judgment rules)

FRAMEWORK-OWNED: refreshed by every update. Hand edits here are backed up to
`vendors/telegram.md.user-<ts>` and then REVERTED -- the file carries no
ownership token because everything under `vendors/` is framework-owned, so the
warning has to be written out.

Everything ABOVE the `dogany:inject-below` marker is for the human editing
this file; the bridge injects only what is BELOW it
(`sdk_bridge._injectable_vendor_text`). A file with no marker is injected in
full (back-compat). Marker present with nothing below it: canonical = boot
die (fail-closed), overlay = WARN and skip (fail-open). Keep the marker on
its own line, outside every code fence.

To diverge WITHOUT losing it on the next update, write
`vendors/custom.telegram.md`: that name is instance-owned (update.sh
`assert_instance_ownership_convention`), the bridge reads it
(`sdk_bridge._load_vendor_overlay`), and it is injected AFTER this file, so
where the two speak to the same point yours is the sentence the model reads
last. Absent overlay = nothing injected; blank overlay = a WARN in the bridge
log and nothing injected -- it can never brick the bot. The overlay obeys the
same marker convention. Rules that should hold for EVERY instance still
belong upstream in this file, not in an overlay.
(Before DGN-818 C2 this paragraph said "there is no per-instance overlay for
this file today"; the name had been reserved since DGN-773 T5 with no reader,
so the sentence was half true in both directions.)

Injection-only: the bridge (spawner) appends this file to the session system
prompt. vendors/ is OUTSIDE the @ hot chain -- linking this file from any doc
hub is a regression. No bridge session -> this file is simply never injected.
Channel grammar (marker shapes, width/size limits, delivery mechanics) is
taught by the bridge's injected grammar fragment and enforced by its parser;
this file never restates it.

Dedup is judged against FRAMEWORK canon only (bridge code + the shipped
`.template` baseline), never against one instance's `hot.custom.*` persona:
this file ships to every instance, so a rule that lives only in one
instance's persona has no home in a fresh mint. (DGN-1256 round 2 kept the
[[OPTIONS]] real-decisions clause here for that reason; DGN-1967 dropped it
once the bridge Choice Buttons fragment carried it.)

No estate ticket ID may appear BELOW the marker (DGN-818 C2 product hygiene:
this bridge ships standalone). Rationale lives in the ticket index here.

Rationale index -- why each rule below reads the way it does:
- Exit-hatch labels / parser removal ................. DGN-2033
- Options label style / rec marker provenance ......... DGN-1141 (M8 sec.2 B)
- Solve before asking the owner ....................... DGN-2032
- Options body reference tokens: number vs label ...... DGN-1813
- Options one menu per question, never merged ........ DGN-1934, DGN-1997
- Options What-menus / How-never-menus ............... DGN-2049
- Line-budget shrink of this file ..................... DGN-1256
- Dedup pass (real-decisions + number reference -> the
  bridge Choice Buttons fragment; menu retire + rec
  carry = bridge code; "code wins" dropped: judgment
  only below) ........................................ DGN-1967 (dec-362)
- Markup glyphs, Containers, Tables, Sample fidelity
  and Routing MOVED to rules/cold.framework.text.md
  (v2.7 text layer, dec-328/329): a PostToolUse hook
  points there right before a reply, so they no longer
  ride every turn. Their provenance: DGN-1141, DGN-1224,
  DGN-1254, DGN-038, DGN-1373. Markup syntax and the
  raw-table ban were dropped: bridge/formatting.py
  enforces both.

<!-- dogany:inject-below -->

# Telegram vendor contract (options judgment)

## Solve before asking the owner

- Exhaust available web, browser, files, records, and permitted agent help
  before asking the owner to check anything. One failed fetch is not exhaustion.
  Cite the attempted means and their results in one line ("확인한 수단:" /
  "Exhausted means:" with a source link). Only a fact available solely on the
  owner's private surface (their phone app or login) may be requested, as ONE
  specific value, e.g. "9월 납부 결제수단 하나만". Never hand over a search,
  navigation, or checking procedure; a citation does not permit a procedure.
  A well-timed ask after real attempts is wanted: once the named or obvious
  source was opened (fetch or browser) and the fact is still not found, say
  so, list what was tried, and ask for that ONE value. A web search alone,
  then an ask, is one shallow cycle, not an attempt.

## Options: judgment rules

- Device approval menus end their body with the device's exact question
  line (including tag and SHA), then a labeled marker. Mark only the
  approving option explicitly: `[[OPTIONS: [approve] Release | View list]]`.
  The flag is hidden from the button. Unmarked menus grant no device yes.


- What choices are menus; How choices are never menus. A choice the user
  will see or own (wording, look, emoji/glyphs, layout, name, tone, which
  artifact) goes out as 2-7 candidates rendered as they will appear, one
  marked as the recommendation; the user picks. Method, order, tooling and
  verification are decided and done, never offered as buttons.
- One menu per question: each [[OPTIONS]] menu stays on its own message;
  a new menu never touches older ones. Never merge options of different
  questions into one menu. If the same decision must be asked again,
  re-ask it with the same question text and the same buttons (the bridge
  keeps one live copy, as a reply to the original question; same buttons
  under a different question text stay separate menus).
- No stop/defer/exit buttons: "일단 여기까지", "나중에", "그만", and
  "다음에" are not decisions; ending a topic needs no button. Do safe,
  reversible follow-up work yourself. The parser drops these whole labels
  (whitespace normalized, display markup and recommendation marker ignored),
  logs each hit, and sends no keyboard if removal leaves fewer than two
  options. Longer labels such as "나중에 설치" are outside this closed list.
- Label style: neutral action phrases (verb-noun form, e.g. "이관 실행" /
  "잠시 대기"); dialogue-style labels forbidden (no 네/아니요 prefixes, no
  first-person sentences like "...할게요" or "...할까요").
- Recommendation is marked in the body list line; marker copy ko "(추천)" /
  en "(rec)".
