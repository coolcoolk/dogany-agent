#!/usr/bin/env python3
"""Kit vocabulary declaration, shared by scaffold and compat-lint (DGN-2145).

terms: "config/terms.json" names the installer-supported payload file.
terms: "none" requires terms_reason: <nonempty explanation> and no file.
The manifest path is relative to payload_root, never the checkout root.

Side (DGN-2149, the C-RELNOTE/C5 precedent in compat-lint.sh): a MISSING
declaration is an authorship defect fixable only at publish time, so the
publish-side gate (d) refuses it; install-side (--install-side) reports it as
SKIP (rc 2) instead of refusing every already-published kit tag cut before
DGN-2145. A declaration that IS present is validated on both sides: the
installer consumes that file, so a broken one is refused everywhere.
"""
import importlib.util
import json
from pathlib import Path
import sys

TERMS_PATH = "config/terms.json"
LOADER = Path(__file__).resolve().parents[3] / "agents/.template/routines/lib/term_dict.py"


def validate(manifest, payload):
    """Return a single-line refusal, or None. Domain packs are unchanged."""
    if manifest.get("kind") != "kit":
        return None
    declared = manifest.get("terms")
    path = Path(payload) / TERMS_PATH
    if declared == "none":
        reason = manifest.get("terms_reason")
        if not isinstance(reason, str) or not reason.strip():
            return "terms: none requires a non-empty terms_reason"
        if path.exists() or path.is_symlink():
            return "terms: none contradicts payload/config/terms.json"
        return None
    if declared != TERMS_PATH:
        return "kit must declare terms: config/terms.json or terms: none with terms_reason"
    if not path.is_file():
        return "declared terms file is missing: payload/config/terms.json"
    if not path.resolve().is_relative_to(Path(payload).resolve()):
        return "terms file escapes payload root"
    try:
        spec = importlib.util.spec_from_file_location("kit_terms_loader", LOADER)
        loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loader)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("terms must be a JSON object")
        loader.validate_layer(dict(data, layer="kit", owner=manifest.get("id")),
                              "kit", manifest.get("id"))
    except (OSError, ValueError, TypeError) as exc:
        return "invalid terms file: " + " ".join(str(exc).splitlines())
    return None


def main(argv):
    install_side = "--install-side" in argv[1:]
    argv = [a for a in argv if a != "--install-side"]
    try:
        path = Path(argv[1])
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("kind") != "kit":
            return 0
        payload = (path.parent / manifest.get("payload_root", "payload")).resolve()
        if not payload.is_relative_to(path.parent.resolve()):
            reason = "payload_root escapes pack boundary"
        elif install_side and "terms" not in manifest:
            print("[compat-lint] SKIP: C-TERMS: install-side -- an undeclared kit vocabulary is a "
                  "publish-time defect the installer cannot fix (publish-side gate (d) is the "
                  "enforcement point); no kit terms layer is installed")
            return 2
        else:
            reason = validate(manifest, payload)
    except (OSError, ValueError, TypeError, IndexError) as exc:
        reason = "cannot validate kit vocabulary: " + " ".join(str(exc).splitlines())
    if reason:
        print("[compat-lint] FAIL: C-TERMS: " + reason)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
