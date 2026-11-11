#!/usr/bin/env python3
"""Seed an explicit confirmed-provisional Why for positive dispatch fixtures.

Only disposable fixture workspaces call this helper. Negative intake replay
uses the unmodified production dispatcher and no fixture default.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'lib'))
from why_net import store

root = Path(sys.argv[1]).resolve()
with store.canonical(root):
    if not store.read_rows(store.EVENTS, root):
        for ev, data in [('propose', {'text': 'Verify the existing supported dispatch flow', 'scope': 'framework'}),
                         ('confirm', {'quote': 'Verify the existing supported dispatch flow'})]:
            store.append_row(store.EVENTS, dict(id='evt-' + ev, why='why-9999', ev=ev,
                                                at='2026-10-28T09:00:00Z', actor='owner', data=data), root)
