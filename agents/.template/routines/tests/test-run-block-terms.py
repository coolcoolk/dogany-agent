#!/usr/bin/env python3
# test-lane: fast
# test-component: dispatch
# test-tier: 0
# test-prereq: tmp-files
# test-new-component: run_block has no direct component suite; dispatch suites exercise it only through macOS rails
"""DGN-2373: every run-block surface honors the owner's actor term."""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
TEMPLATE = os.path.dirname(os.path.dirname(HERE))
LIB = os.path.join(TEMPLATE, "routines", "lib")
sys.path.insert(0, LIB)

import run_block  # noqa: E402


def rows(root):
    notice = run_block.job_lines("job", "subagent", "Claude Sonnet 5.5",
                                 clock="13:46", root=root, lang="ko")
    end = run_block.job_lines("job", "subagent", "Claude Sonnet 5.5",
                              clock="13:52", elapsed="6분", root=root,
                              lang="ko")
    board = run_block.board_job_lines(
        "job", "subagent", "Claude Sonnet 5.5", stamp="13:46 (6분~)",
        root=root, lang="ko")
    return notice[1], end[1], board[1]


with tempfile.TemporaryDirectory(prefix="dgn2373.") as root:
    shutil.copytree(os.path.join(TEMPLATE, "config", "i18n"),
                    os.path.join(root, "config", "i18n"))
    default = rows(root)
    assert default == (
        "• 서브에이전트: Claude Sonnet 5.5, 13:46",
        "• 서브에이전트: Claude Sonnet 5.5, 13:52 (6분)",
        "• 서브에이전트: Claude Sonnet 5.5",
    ), default

    local = os.path.join(root, "config", "i18n.local")
    os.makedirs(local)
    with open(os.path.join(local, "ko.json"), "w", encoding="utf-8") as fh:
        json.dump({"term.subagent": "서브에이전트"}, fh, ensure_ascii=False)
    renamed = rows(root)
    assert renamed == (
        "• 서브에이전트: Claude Sonnet 5.5, 13:46",
        "• 서브에이전트: Claude Sonnet 5.5, 13:52 (6분)",
        "• 서브에이전트: Claude Sonnet 5.5",
    ), renamed

    print("default START / END / workbench:\n" + "\n".join(default))
    print("renamed START / END / workbench:\n" + "\n".join(renamed))

print("RESULT: PASS=6 FAIL=0")
