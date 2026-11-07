"""DGN-1914: on-demand local speech-to-text install.

install.sh no longer asks about voice. faster-whisper (the package) is a core
bridge dependency, but the model weights are not fetched at install time.
Before this module, the first voice message silently triggered a hidden
HuggingFace download of config.py's default model ("small") inside the
transcription thread, with no feedback to the owner (offline -> a generic
transcription failure).

Now the bridge asks first: when no model is configured and none is cached
locally, the voice message is held, the owner gets one install button, and a
tap installs the spec-recommended model (the DGN-146 tiers, formerly
install.sh whisper_reco), records LOCAL_WHISPER_MODEL in the instance .env,
and the held voice messages are transcribed.

Follow-up (owner decision 2026-10-07 20:40): the owner picks the tier.  The
offer lists only the tiers this device can run, marks the recommended one and
the slow ones, and falls back to the single install button when one tier is
left.  The footer's "change voice recognition" phrase re-opens the picker later.

Follow-up 2 (owner dec-312, 2026-10-08 06:19): two tiers only, fast / accurate =
small / large-v3-turbo, each body line names the model.  The accuracy claim is
locale-aware: it names the language only when the voice language matches the UI
catalog language, otherwise the neutral "_desc_generic" copy is used.

Existing installs with LOCAL_WHISPER_MODEL set (instance .env, machine
voice.conf or the shared env) are untouched: an explicit model is never gated.
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

from bridge.config import PROJECT_ENV_PATH, config

logger = logging.getLogger(__name__)

ENV_KEY = "LOCAL_WHISPER_MODEL"
CALLBACK_PREFIX = "vstt:"
INSTALL_CALLBACK = "vstt:install"
# DGN-1914 follow-up: tier picker buttons carry "vstt:pick:<model>".
PICK_PREFIX = "vstt:pick:"

# Picker order: fastest first. Labels/names/descriptions live in the i18n
# catalogs under voice_tier_<key>_*.
TIERS = ("small", "large-v3-turbo")
TIER_KEYS = {"small": "small", "large-v3-turbo": "turbo"}

# Comfortable thresholds (DGN-146, same as recommend_model): RAM >= GB and
# free disk >= GB.  Below the RAM one a shown tier is marked slow.
COMFORT = {"small": (0, -1), "large-v3-turbo": (16, 6)}
# Hard minimums: below either the tier is hidden.  Disk = download size plus
# ~2GB headroom, rounded up (large-v3-turbo 1.6+2).  small is the floor and is
# always offered (a failed download reports itself and offers retry).
HARD_MIN = {"small": (0, 0), "large-v3-turbo": (8, 4)}

# faster-whisper (CTranslate2) approx on-disk sizes, stated conservatively.
# medium / large-v3 are no longer offered but stay for existing installs.
# faster-whisper (>= 1.1) resolves "large-v3-turbo" itself, no mapping needed.
MODEL_SIZES = {"small": "0.5GB", "medium": "1.5GB", "large-v3": "3GB",
               "large-v3-turbo": "1.6GB"}

_GB = 1024 ** 3


def explicit_model() -> str:
    """The configured model name, or "" when only config.py's default applies."""
    return os.environ.get(ENV_KEY, "").strip()


def package_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def model_cached(name: str) -> bool:
    """True when the model's weights are already in the local cache."""
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        return False
    try:
        download_model(name, local_files_only=True)
    except Exception:
        return False
    return True


def needs_install(name: Optional[str] = None) -> bool:
    """True only when the owner must be asked before transcribing.

    An explicit model, or a missing package (the existing VOICE_UNAVAILABLE
    path reports that), never asks. Otherwise ask unless already cached.
    """
    if explicit_model() or not package_available():
        return False
    return not model_cached(name or config.local_whisper_model)


def system_ram_gb() -> Optional[int]:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // _GB
    except (ValueError, OSError, AttributeError):
        return None


def disk_free_gb(path: Optional[Path] = None) -> Optional[int]:
    try:
        return shutil.disk_usage(str(path or Path.home())).free // _GB
    except OSError:
        return None


def recommend_model(ram_gb: Optional[int], disk_gb: Optional[int]) -> str:
    """large-v3-turbo when it runs comfortably (RAM >=16GB and free disk
    >=6GB, COMFORT); anything else, unknown RAM included -> small.  Agrees
    with tier_options.  (DGN-146 medium / large-v3 tiers retired by dec-312.)"""
    if ram_gb is None:
        return "small"
    min_ram, min_disk = COMFORT["large-v3-turbo"]
    if ram_gb >= min_ram and (disk_gb or 0) >= min_disk:
        return "large-v3-turbo"
    return "small"


def pick_model() -> str:
    return recommend_model(system_ram_gb(), disk_free_gb())


class TierOption(NamedTuple):
    model: str
    slow: bool = False
    recommended: bool = False


def tier_options(ram_gb: Optional[int], disk_gb: Optional[int]) -> List[TierOption]:
    """The tiers the picker shows, fastest first.

    Hidden: RAM or disk below the tier's hard minimum.  Slow: runs, but RAM is
    below the comfortable threshold.  Recommended: the most accurate tier that
    runs comfortably (== recommend_model).  Device info unreadable -> small
    only.  A single option means the plain one-button install flow.
    """
    if ram_gb is None or disk_gb is None:
        return [TierOption("small", recommended=True)]
    best = recommend_model(ram_gb, disk_gb)
    out = []
    for model in TIERS:
        min_ram, min_disk = HARD_MIN[model]
        if ram_gb < min_ram or disk_gb < min_disk:
            continue
        out.append(TierOption(model, slow=ram_gb < COMFORT[model][0],
                              recommended=model == best))
    return out


def current_tier_options() -> List[TierOption]:
    return tier_options(system_ram_gb(), disk_free_gb())


def _default_tr(key: str) -> str:
    from bridge.i18n import t
    return t(key)


def tier_label(model: str, tr: Callable[[str], str] = _default_tr) -> str:
    return tr("voice_tier_%s_label" % TIER_KEYS[model])


def install_prompt(model: str, tr: Callable[[str], str] = _default_tr) -> str:
    """The one-button install offer, naming the model the tap installs and
    its download size (dec-321).  An untiered model shows its raw id."""
    key = TIER_KEYS.get(model)
    name = tr("voice_tier_%s_name" % key) if key else model
    return tr("voice_install_prompt").format(name=name, size=MODEL_SIZES.get(model, "?"))


def language_matches(catalog_lang: Optional[str], voice_lang: Optional[str]) -> bool:
    """True when the voice transcription language is the catalog's language
    (ko UI + ko voice, en UI + en voice).  Unset / auto voice language is
    resolved by config (it follows LOCALE), so None here means "auto"."""
    if not catalog_lang or not voice_lang:
        return False
    norm = lambda x: str(x).strip().lower().split("_")[0].split("-")[0]
    return norm(catalog_lang) == norm(voice_lang)


def render_picker(options: List[TierOption],
                  tr: Callable[[str], str] = _default_tr,
                  catalog_lang: Optional[str] = None,
                  voice_lang: Optional[str] = None) -> str:
    """Picker body: intro, one line per SHOWN tier, footer.

    catalog_lang is the language of `tr` (default: the UI locale); voice_lang
    the configured transcription language (default: config.whisper_language).
    The language-specific description is used only when they match."""
    if catalog_lang is None:
        catalog_lang = config.locale
    if voice_lang is None:
        voice_lang = config.whisper_language
    suffix = "" if language_matches(catalog_lang, voice_lang) else "_generic"
    lines = [tr("voice_pick_prompt")]
    for opt in options:
        key = TIER_KEYS[opt.model]
        speed = tr("voice_pick_slow") if opt.slow else tr("voice_tier_%s_speed" % key)
        desc = " ".join(tr("voice_tier_%s_desc%s" % (key, suffix)).format(speed=speed).split())
        line = tr("voice_pick_line").format(
            label=tr("voice_tier_%s_label" % key), name=tr("voice_tier_%s_name" % key),
            desc=desc, size=MODEL_SIZES[opt.model],
        )
        if opt.recommended:
            line += tr("voice_pick_recommended")
        lines.append(line)
    lines.append(tr("voice_pick_footer"))
    return "\n".join(lines)


def _norm(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def is_change_request(text: str, tr: Callable[[str], str] = _default_tr) -> bool:
    """True when the whole message asks to re-pick the STT tier (the phrase
    the picker footer tells the owner to say; spacing/punctuation ignored)."""
    if not text or len(text) > 40:
        return False
    norm = _norm(text)
    return any(norm == _norm(p) for p in tr("voice_change_phrases").split("|") if p.strip())


def persist_model(name: str, env_path: Optional[Path] = None) -> None:
    """Set LOCAL_WHISPER_MODEL in the instance .env (replace or append),
    atomically and keeping the file mode; then apply it to this process."""
    path = Path(env_path or PROJECT_ENV_PATH)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        lines, mode = [], 0o600
    entry = "%s=%s" % (ENV_KEY, name)
    out, done = [], False
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key == ENV_KEY:
            if not done:
                out.append(entry)
                done = True
            continue
        out.append(line)
    if not done:
        out.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".voice-tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    os.environ[ENV_KEY] = name
    config.local_whisper_model = name


def install_model(name: str) -> None:
    """Blocking: download the model weights, then record the choice.
    Run it off the event loop (asyncio.to_thread)."""
    from faster_whisper.utils import download_model

    logger.info("voice: installing local STT model %s", name)
    download_model(name)
    persist_model(name)
    logger.info("voice: local STT model %s installed", name)
