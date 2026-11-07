"""/model picker text + buttons (DGN-1814 r3/r4, owner copy final 2026-10-01 09:52).

Pure render layer: bot.py sends what this returns. Names and versions come
from the ONE model table (routines/lib/model_display.py over
dispatch-model-display.tsv, read layered vendor -> family -> version); the
bridge never carries a version of its own.

One chat-selectable vendor -> one step:
    header / "Current: <model>[ hint]" / switch note, one button per family
    with the full name ("<Vendor> <Family> <version>", the current one + the i18n
    model_current_mark).
Two or more -> two steps: the same header + vendor buttons ("Claude" marked,
"Codex"), then that vendor's family buttons.

"Current:" is the model that really answered (bridge/live_model.py, this
process only) when it is the configured family; otherwise the configured
name WITHOUT a version -- never a guessed or stale version. When the live
version differs from the table's for a configured alias, the latest-version
hint follows (a restart re-resolves the alias and may switch to it).
"""

import importlib.util
import logging
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from bridge.config import PROJECT_ROOT
from bridge.i18n import t

logger = logging.getLogger(__name__)

# The runtime the bridge itself drives (Claude Agent SDK). Its table rows map
# the BRIDGE_MODELS aliases; other chat vendors are listed but not wired.
NATIVE_RUNTIME = "claude-code"
# Vendor word used only when the table cannot be read (bridge shipped alone).
NATIVE_VENDOR = "Claude"
CB_MODEL = "model:"      # model:<alias>              native family switch
CB_VENDOR = "modelv:"    # modelv:<vendor>            two-step: open a vendor
CB_FOREIGN = "modelx:"   # modelx:<vendor>:<alias>    family of an unwired vendor

Button = Tuple[str, str]  # (label, callback_data)

_lib_cache: Dict[str, object] = {}


def _lib_dirs() -> List[Path]:
    return [Path(PROJECT_ROOT) / "routines" / "lib",
            Path(__file__).resolve().parents[1] / "routines" / "lib"]


def _lib():
    """routines/lib/model_display.py as a module, or None (cached)."""
    if "mod" in _lib_cache:
        return _lib_cache["mod"]
    mod = None
    for d in _lib_dirs():
        path = d / "model_display.py"
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("model_display", str(path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            break
        except Exception:  # noqa: BLE001 - presentation only
            logger.warning("model picker: cannot load %s", path, exc_info=True)
            mod = None
    _lib_cache["mod"] = mod
    return mod


def chat_table() -> Dict[str, Dict[str, dict]]:
    """{vendor: {family: entry}} for the chat surface; {} when unreadable."""
    mod = _lib()
    try:
        return mod.model_table(surface="chat") if mod else {}
    except Exception:  # noqa: BLE001
        logger.warning("model picker: model table unreadable", exc_info=True)
        return {}


def _split(display: str) -> Tuple[str, str, str]:
    mod = _lib()
    if mod is not None:
        return mod.split_display(display)
    words = (display or "").split()
    return (words[0] if words else "", " ".join(words[1:2]), " ".join(words[2:]))


def _parse_id(model_id: str) -> Optional[str]:
    mod = _lib()
    try:
        return mod.parse_model_id(model_id) if mod else None
    except Exception:  # noqa: BLE001
        return None


def _norm(mid: str) -> str:
    return re.sub(r"\[[^\]]*\]$", "", (mid or "").strip().lower())


def native_vendor(table: dict) -> str:
    for vendor, families in table.items():
        if any(e.get("runtime") == NATIVE_RUNTIME for e in families.values()):
            return vendor
    return NATIVE_VENDOR


def vendors(table: dict) -> List[str]:
    """Chat-selectable vendors, table order; the native one always present."""
    out = list(table)
    native = native_vendor(table)
    if native not in out:
        out.insert(0, native)
    return out


def _native_entry(alias: str, table: dict) -> Optional[Tuple[str, str, dict]]:
    for vendor, families in table.items():
        for family, entry in families.items():
            if entry.get("runtime") == NATIVE_RUNTIME and entry.get("alias") == alias:
                return vendor, family, entry
    return None


def describe(model: str, table: dict) -> Tuple[str, str, str]:
    """(vendor, family, version) of a configured model (alias or full id)."""
    hit = _native_entry(model, table)
    if hit:
        return hit[0], hit[1], hit[2].get("version", "")
    parsed = _parse_id(model) if model.startswith("claude-") else None
    if parsed:
        return _split(parsed)
    return native_vendor(table), (model[:1].upper() + model[1:]), ""


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def full_name(model: str, table: dict) -> str:
    """"<Vendor> <Family> <version>" -- switched / already-active lines."""
    return _join(*describe(model, table))


def _live_same(configured: str, live_id: str, table: dict) -> Optional[str]:
    """Display of the live model when it is the configured one, else None."""
    vendor, family, _version = describe(configured, table)
    live = _parse_id(live_id) if live_id else None
    if not live:
        return None
    lv, lf, _lver = _split(live)
    if configured.startswith("claude-"):
        same = _norm(configured) == _norm(live_id)
    else:
        same = lv.lower() == vendor.lower() and lf.lower() == family.lower()
    return live if same else None


def now_name(configured: str, live_id: str, table: dict) -> str:
    """The "Current:" name: the live model when it is the configured one,
    else the configured vendor + family without a version."""
    live = _live_same(configured, live_id, table)
    if live:
        return live
    vendor, family, _version = describe(configured, table)
    return _join(vendor, family)


# Korean digit readings ending in a final consonant (0 yeong, 1 il, 3 sam,
# 6 yuk, 7 chil, 8 pal) take the copula after a consonant. self_restart.sh
# uses [036] because its particle (-euro) treats a final l as a vowel.
_BATCHIM_DIGITS = "013678"


def latest_hint(configured: str, live_id: str, table: dict) -> str:
    """" (latest is <ver>; ...)" when an alias's live version is not the
    table's; "" otherwise (pinned full id, other family, unknown live)."""
    if configured.startswith("claude-"):
        return ""
    live = _live_same(configured, live_id, table)
    table_version = describe(configured, table)[2]
    if not live or not table_version:
        return ""
    if _split(live)[2] == table_version:
        return ""
    key = ("model_latest_hint_batchim" if table_version[-1:] in _BATCHIM_DIGITS
           else "model_latest_hint")
    return t(key).format(version=table_version)


def _label(vendor: str, family: str, version: str, current: bool) -> str:
    return _join(vendor, family, version) + (t("model_current_mark") if current else "")


def _header(current: str, live_id: str, table: dict) -> str:
    now = now_name(current, live_id, table) + latest_hint(current, live_id, table)
    return "\n".join([t("model_select"), t("model_now").format(model=now),
                      t("model_switch_note")])


def _native_buttons(current: str, whitelist: Iterable[str], rank: Dict[str, int],
                    table: dict) -> List[Button]:
    names = list(dict.fromkeys(whitelist))
    if current not in names:
        names.append(current)
    names.sort(key=lambda n: (rank.get(n, 99), n))
    out = []
    for name in names:
        vendor, family, version = describe(name, table)
        out.append((_label(vendor, family, version, name == current), CB_MODEL + name))
    return out


def family_step(vendor: str, current: str, whitelist: Iterable[str],
                live_id: str, rank: Dict[str, int],
                table: Optional[dict] = None) -> Tuple[str, List[Button]]:
    """Header + family buttons of one vendor (the one-step picker, or step 2)."""
    table = chat_table() if table is None else table
    text = _header(current, live_id, table)
    if vendor == native_vendor(table):
        return text, _native_buttons(current, whitelist, rank, table)
    buttons = [(_label(vendor, family, e.get("version", ""), False),
                "%s%s:%s" % (CB_FOREIGN, vendor, e.get("alias", "")))
               for family, e in table.get(vendor, {}).items()]
    return text, buttons


def render(current: str, whitelist: Iterable[str], live_id: str,
           rank: Dict[str, int], table: Optional[dict] = None
           ) -> Tuple[str, List[Button]]:
    """The /model reply: one step for one chat vendor, vendor step for more."""
    table = chat_table() if table is None else table
    names = vendors(table)
    if len(names) < 2:
        return family_step(names[0], current, whitelist, live_id, rank, table)
    cur_vendor = describe(current, table)[0]
    text = _header(current, live_id, table)
    mark = t("model_current_mark")
    return text, [(v + (mark if v == cur_vendor else ""), CB_VENDOR + v)
                  for v in names]
