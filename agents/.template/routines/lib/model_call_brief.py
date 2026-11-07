#!/usr/bin/env python3
"""Shared classification: does a dispatch brief require model calls?

A lane without model-call capability (codex local/cloud) cannot run such a
brief, so it must not be a replacement target. One matcher, used by the
dispatcher shell (CLI: exit 0 = needs model calls) and tests.
"""
import re
import sys

_MODEL = r'(?:haiku|sonnet|opus|fable|claude)'
_PATTERNS = [
    # "Task subagents", "Agent subagent", "Agent model calls", "Agent tool"
    r'\b(?:agent|task)[\s/_-]*(?:tool|subagents?|model[\s-]*calls?)\b',
    r'\bmodel[\s-]+(?:subagents?|calls?)\b',
    # "Agent tool with model haiku / sonnet", "call the Haiku model"
    r'\b(?:agent|task)\b[^\n.]{0,40}\bmodel\s*[:=]?\s*' + _MODEL + r'\b',
    r'\b(?:call|invoke|use|spawn)\w*\b[^\n.]{0,30}\b' + _MODEL + r'\b[^\n.]{0,10}\bmodel\b',
    r'\bmodel[\s=:]+' + _MODEL + r'\s*/\s*' + _MODEL + r'\b',
]
_RX = [re.compile(p, re.I) for p in _PATTERNS]


def needs_model_calls(text):
    return any(r.search(text) for r in _RX)


if __name__ == '__main__':
    with open(sys.argv[1], encoding='utf-8', errors='replace') as f:
        sys.exit(0 if needs_model_calls(f.read()) else 1)
