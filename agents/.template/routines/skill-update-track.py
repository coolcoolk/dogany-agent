#!/usr/bin/env python3
"""Capture instruction hashes at prompt entry and before every tool."""
import importlib.util
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).absolute().parent / 'lib'))
from hook_paths import instance_root


def main():
    data = json.load(sys.stdin)
    try:
        spec = importlib.util.spec_from_file_location('track_skill_layer',
                                                     Path(__file__).with_name('skill-layer.py'))
        layer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(layer)
        session, turn = layer.update_context(data)
        layer.capture_update_turn(instance_root(), session, turn)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reason = 'Instruction snapshot unavailable: ' + str(exc)
        print(json.dumps({'systemMessage': reason}))


if __name__ == '__main__':
    main()
