---
name: dogany-workbench-plugin
display_name: 작업대 칸 추가/숨기기 (로컬 오버라이드)
scope: framework
layer: shipped
description: Add, change or hide a block on the pinned workbench board through the local board plugin instead of patching the framework footer. The workbench = the message pinned at the top of the chat; users call it by what they see (고정 메시지, 고정핀, 상단 고정, 공지, 핀 메시지). Fires when the owner asks for a custom workbench block or hides a base one. Korean triggers - "작업대에 X 칸 넣어줘", "작업대 커스텀", "작업대에 계획 블록 추가", "작업대에서 완료 칸 빼줘", "작업대 로컬 오버라이드", "고정 메시지에 X 넣어줘", "고정핀에 칸 추가", "상단 고정에 계획 보여줘", "공지에 진행상황 넣어줘", "핀 메시지 바꿔줘". English triggers - "add a block to the workbench", "customize the workbench board", "hide a workbench section", "board plugin". Never edit routines/status-footer.py for this (updates overwrite it). Tier BEST-EFFORT.
---

# dogany-workbench-plugin -- custom workbench blocks via the board plugin

Reader: agent. Tier: BEST-EFFORT (description auto-fire, no hook).
"Workbench" = the message pinned at the top of the chat; users say 고정 메시지 / 고정핀 / 공지 / 핀 메시지 for it.
Request like "put an X block on the workbench" / "작업대에 X 칸 넣어줘": do NOT
patch `routines/status-footer.py`. The board has ONE extension seam, a board
plugin (API v1: conf `DASHBOARD_BOARD_PLUGIN`, `build_sections` -> sections /
hide), and your own plugin file survives every update.

## 1. Where the plugin lives (instance-owned)
`data/board-plugin/local_sections.py` -- `data/` is agent-owned, never
cleaned by updates, and backed up nightly with the DB dump.
Do not put it in `routines/` (framework scripts, refreshed by updates) or in
`routines/bundle/` (a kit/pack payload dir: a pack can land a file of the
same name there).

## 2. Turn it on (conf key)
In `config/agent.conf` (yours, never reset by updates):

    DASHBOARD_BOARD_PLUGIN=data/board-plugin/local_sections.py

Relative = from the instance root; absolute also works; `none` disables.
Env `DOGANY_BOARD_PLUGIN` wins over the conf key. Unset = the pack default
`routines/bundle/workbench_sections.py` (what a kit like devkit ships).
The board picks the change up on the next Stop; no restart.

## 3. The contract
The file declares `PLUGIN_API = 1` and `build_sections(ctx)` returning

    {"sections": [{"key": ..., "lines": [...]}], "hide": [...], "ok": True}

- `key`: `^[a-z0-9_-]{1,32}$`, unique; an invalid or repeated key drops that
  one section (the first of a repeated key wins).
- `lines` (or `variants`: lists of lines, largest first; the length cut
  steps down to a smaller one). `trigger: True` lets the section alone put
  the board up; `pinned: True` exempts it from the length cut (must stay
  under 1200 UTF-16 units).
- `hide`: only the base sections `recent_done`, `unpark`, `console_actions`.
- `ok: False` = "my collector failed" so an empty board is not read as
  confirmed-empty. Return it instead of raising.
- `ctx["host"]` is the footer module: reuse `_conf_get`, `_ROOT_DIR`,
  `_iter_worklog_tickets`, `_ticket_id` ... rather than copying them.
  `ctx` also has `live_descs`, `live_rows`, `completed_descs`,
  `review_descs`, `decisions`. Read only; never write from a plugin.

Fail-open: a missing, broken, raising or wrong-API plugin leaves the base
board exactly as without it.

Section look that matches the built-in areas: a bold title line, then the
rows inside one quote:

```python
PLUGIN_API = 1


def build_sections(ctx):
    rows = ["rolling plan A", "rolling plan B"]
    return {"ok": True, "sections": [{"key": "master_plan", "lines": (
        ["**[Master plan]**", "quote::"] + rows + ["quote::end"])}]}
```

## 4. What stays framework-owned
The top line (agent emoji + date/time + freshness stamp), the live section's
background-jobs block, `[결정대기]` decisions and the length cut. A plugin
adds sections and may hide the three base keys above; it cannot edit these.

## 5. A kit/pack already ships a plugin: wrap it, do not replace it
Pointing `DASHBOARD_BOARD_PLUGIN` at your file REPLACES the kit's plugin
(only one file is loaded), so its sections vanish. Compose instead: load the
kit file from your plugin, call it, and append your sections.

```python
import importlib.util
import os

PLUGIN_API = 1
KIT_PLUGIN = os.path.join("routines", "bundle", "workbench_sections.py")


def _kit(host):
    path = os.path.join(host._ROOT_DIR, KIT_PLUGIN)
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("kit_board_plugin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if getattr(mod, "PLUGIN_API", None) != 1:
        raise RuntimeError("kit plugin API mismatch")
    return mod


def build_sections(ctx):
    sections, hide, ok = [], [], True
    try:
        kit = _kit(ctx["host"])
        res = kit.build_sections(ctx) if kit else {}
        sections = list(res.get("sections") or [])
        hide = list(res.get("hide") or [])
        ok = res.get("ok", True) is not False
    except Exception:
        ok = False  # the kit failed; still show ours
    sections.append({"key": "master_plan", "lines": (
        ["**[Master plan]**", "quote::", "rolling plan A", "quote::end"])})
    return {"ok": ok, "sections": sections, "hide": hide}
```

Rules: kit sections first, yours after; pick a key the kit does not use (a
repeated key is dropped, first wins); pass the kit's `hide` through; carry
its `ok` through. Delete the wrapper's kit part only if the kit is removed.

## 6. Check it
Set `DOGANY_DASHBOARD_DEBUG=1` and run the footer once; a rejected plugin
prints one `board-plugin: ...` line on stderr (import failed / API mismatch /
no build_sections / raised).
