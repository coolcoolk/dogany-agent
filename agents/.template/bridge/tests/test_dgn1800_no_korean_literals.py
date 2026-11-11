"""DGN-1800: code = English/ASCII -- owner copy lives in the locale tables.

Fails on any Hangul string literal (docstrings included) in a non-test bridge
.py outside the copy table (i18n/ko.py) and the explicit allowlist below.
Allowlisted literals are NOT owner copy moved out of place: they MATCH owner
or model speech (regex / corpus), are i18n-backed fallbacks, a lockstep
contract, or a module with no call site.  Add an entry only with a reason.
"""

import ast
import os
import re
from pathlib import Path

for _name in list(os.environ):
    # ESTATE:BEGIN isolate inherited estate settings as well as Git configuration
    if _name.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
    # ESTATE:ELSE
    # if _name.startswith("GIT_CONFIG_"):
    # ESTATE:END
        del os.environ[_name]

BRIDGE = Path(__file__).resolve().parents[1]
HANGUL = re.compile("[가-힣ㄱ-ㆎ]")

# The copy table itself.
COPY_TABLES = {"i18n/ko.py"}

# Whole files: dormant /health module (DGN-1435 pulled it off the command
# surface; no call site until the rewrite, which must move its copy).
ALLOW_FILES = {"healthcmd.py"}

# (file, function-or-<module>, literal) -- exact literal text.
_PARTICLES = ("에서는", "로부터", "으로는", "이라도", "이지만", "에서", "부터",
              "까지", "처럼", "보다", "라도", "조차", "마저", "밖에", "이나",
              "마다", "커녕", "한테", "에게", "는", "은", "이", "가", "을", "를",
              "의", "도", "만", "나", "에", "로", "와", "과", "뿐")
ALLOW = {
    # DGN-1400 leaked-turn match corpus (grammar particles + Hangul classes).
    *(("formatting.py", p) for p in _PARTICLES),
    ("formatting.py", "[가-힣]+"),
    ("formatting.py", ")(?=[가-힣])"),
    # DGN-851 zero-delta fallbacks: the copy is read from i18n; these apply
    # only when bridge.i18n is unimportable (push.sh sanitize hop, DGN-822).
    ("formatting.py", "⋯ 중략 ⋯"),
    ("formatting.py", "진행 기록"),
    ("formatting.py", "중단됨 · 진행 기록"),
    ("formatting.py", "시간 초과 · 진행 기록"),
    ("formatting.py", "…(생략)"),
    ("formatting.py", "중단됨"),
    # DGN-1683 lockstep copy of the mint-agent OWNER-SAY block (ASCII source).
    ("mint_gate.py", "화면 전달 상태를 확인하지 못했어요. 잠시 후 다시 요청해 주세요."),
    # Model-output match regexes (footer strip, DGN-086 placeholder flake,
    # Hangul-presence probe).
    ("sdk_bridge.py", r"^\[(?:라이브|결정대기)\][^\n]*(?:\n+(?:- [^\n]*|\[(?:라이브|결정대기)\][^\n]*))*\s*\Z"),
    ("sdk_bridge.py", "[가-힣]"),
    # DGN-1951: interrogative endings matched in pushed text (outbound
    # context: a push that asks without a question mark still registers).
    ("outbound_context.py",
     "(?:\uae4c\uc694?|\ub098\uc694|\uac00\uc694|\ub294\uac00|\uc778\uac00"
     "|\ub0d0|\ub294\uc9c0|\uc744\uc9c0|\ub420\uc9c0|\ud560\uc9c0|\uac74\uc9c0)$"),
    # DGN-1927 alternative-question shape: matches the model's Korean question
    # endings / wh-words / clause joiner, never shown to the owner.
    ("options.py", r"(?:세요|어요|아요|해요|나요|까요|예요|이에요|인가요)\s*$"),
    ("options.py", r"뭐|무엇|어디|언제|왜|어떻게|누가|누구|얼마|몇"),
    ("options.py", r"\s*,\s+|\s+아니면\s+|\s+or\s+"),
    # DGN-2033 closed exit-hatch set: matched against model-authored option
    # labels and dropped, never shown to the owner (parity-pinned with
    # routines/lib/output_gate.py EXIT_HATCH_LABELS).
    ("options.py", "일단 여기까지"),
    ("options.py", "나중에"),
    ("options.py", "그만"),
    ("options.py", "다음에"),
}
# The configured helper noun is interpolated; only this exact static match
# suffix contains Hangul. It detects model speech, never owner copy.
ALLOW.add(("sdk_bridge.py", '(?:\uc774|\uac00)?\\s*(\uc544\uc9c1\\s*)?\uc791\uc5c5\\s*\uc911|\uc11c\ube0c\uc5d0\uc774\uc804\ud2b8\\s*(\uc2e4\ud589|\uc791\uc5c5)\\s*\uc911|\uc644\ub8cc\\s*(\uc54c\ub9bc|\ud1b5\ubcf4)\\S*\\s*(\ub300\uae30|\uc624\uba74)|\ubc31\uadf8\ub77c\uc6b4\ub4dc\\s*(\uc815\ucc30\\s*)?\uc5d0\uc774\uc804\ud2b8\\s*\uc644\ub8cc\\s*\ub300\uae30)'))
# Docstrings that quote owner copy / Korean syntax as documentation.
ALLOW_DOC = {"formatting.py", "i18n/__init__.py", "machine_gate.py", "options.py", "sdk_bridge.py"}


def _docstrings(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _hits():
    hits = []
    for path in sorted(BRIDGE.glob("**/*.py")):
        rel = path.relative_to(BRIDGE).as_posix()
        if rel.startswith("tests/") or "/tests/" in rel or rel in COPY_TABLES or rel in ALLOW_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if not HANGUL.search(node.value):
                continue
            if id(node) in docs and rel in ALLOW_DOC:
                continue
            if (rel, node.value) in ALLOW:
                continue
            hits.append("%s:%d %r" % (rel, node.lineno, node.value[:60]))
    return hits


def test_no_korean_string_literal_outside_copy_tables():
    hits = _hits()
    assert not hits, "Korean literal outside the i18n copy table:\n" + "\n".join(hits)


# ESTATE:BEGIN DGN-1417 the update_* keys live in the i18n update-command region, which the public tree does not ship
def test_moved_update_copy_is_byte_identical_ko():
    import runpy
    from types import SimpleNamespace
    en, ko = (SimpleNamespace(**runpy.run_path(str(BRIDGE / "i18n" / (lang + ".py"))))
              for lang in ("en", "ko"))
    assert ko.STRINGS["update_stale"] == "업데이트할 내용이 달라졌어요. 새 목록에서 다시 골라주세요."
    assert ko.STRINGS["update_check_failed"] == "업데이트 확인에 실패했어요.\n· 이유: 원인은 아직 확인되지 않았어요.\n· 대안: /update로 다시 시도해 주세요."
    for key in ("update_check_failed", "update_lookup_failed", "update_lookup_failed_again"):
        assert len(ko.STRINGS[key].splitlines()) == 3
        assert "· 이유: 원인은 아직 확인되지 않았어요." in ko.STRINGS[key]
    assert ko.STRINGS["update_consent_prefix"].format(labels="메인 에이전트") == "메인 에이전트이 함께 업데이트돼요. 잠시 답변을 멈췄다가 돌아올게요."
    assert ko.STRINGS["update_consent_button"] == "잠시 멈추고 업데이트"
    assert ko.STRINGS["update_apply_button"] == "적용"
    for key in ("update_stale", "update_check_failed", "update_consent_prefix",
                "update_consent_button", "update_apply_button"):
        assert en.STRINGS[key] and not HANGUL.search(en.STRINGS[key])
# ESTATE:END
