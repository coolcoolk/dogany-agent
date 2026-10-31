"""DGN-816: footer stripper over-deletion regression tests.

The old _FOOTER_BLOCK_RE matched a [라이브]/[결정대기] marker ANYWHERE in the
body (no line-start / message-end anchor) and consumed everything up to the
next '[' via [^\\[]*, so a legitimate mid-body mention of those bracket
strings deleted the rest of the user's message ("cut off mid-sentence" loss,
owner-observed 2026-08-10).

Fixed contract:
  (a) mid-body occurrences of the literal strings [라이브] / [결정대기] /
      [[OPTIONS]] are delivered intact -- no truncation;
  (b) a real trailing canonical footer block (status-footer.py _build_footer
      shape: bare marker header lines + "- " bullets, at the message tail;
      legacy one-line form included) is still stripped exactly once.
"""

import json

import pytest

from bridge import sdk_bridge
from bridge.sdk_bridge import _FOOTER_BLOCK_RE, _consume_footer_sidecar


# ---------------------------------------------------------------------------
# Sidecar fixture: _consume_footer_sidecar reads/clears the sidecar file at
# module constant _FOOTER_SIDECAR (under the hermetic PROJECT_ROOT tmpdir).
# ---------------------------------------------------------------------------

@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    """Point the module at a per-test sidecar file; return a writer helper."""
    path = tmp_path / "footer-sidecar.json"
    monkeypatch.setattr(sdk_bridge, "_FOOTER_SIDECAR", path)

    def write(footer):
        path.write_text(
            json.dumps({"footer": footer, "ts": 1}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    return write


CANONICAL_FOOTER = "[라이브]\n- 배포 동생\n[결정대기]\n- dec-101 릴리즈 승인"


# ---------------------------------------------------------------------------
# (a) mid-body literals are preserved -- the over-deletion regression
# ---------------------------------------------------------------------------

class TestMidBodyPreserved:
    def test_midbody_live_literal_not_truncated(self, sidecar):
        sidecar("")
        content = (
            "풋터의 [라이브] 표기는 대시보드로 옮겼습니다.\n"
            "이 문장은 잘리면 안 됩니다."
        )
        assert _consume_footer_sidecar(content) == content

    def test_midbody_decision_literal_not_truncated(self, sidecar):
        sidecar("")
        content = (
            "다음 항목이 [결정대기] 상태로 넘어갑니다: dec-101.\n"
            "뒤 문장 유지 확인."
        )
        assert _consume_footer_sidecar(content) == content

    def test_options_marker_preserved(self, sidecar):
        sidecar("")
        content = (
            "어느 쪽으로 할까요?\n"
            "1. 이관 실행\n"
            "2. 잠시 대기\n"
            "[[OPTIONS]]"
        )
        assert _consume_footer_sidecar(content) == content

    def test_line_start_marker_followed_by_prose_preserved(self, sidecar):
        # A line STARTING with the marker but followed by ordinary prose is
        # body text, not a footer block -- nothing may be stripped.
        sidecar("")
        content = (
            "[라이브] 섹션의 의미를 설명드리면:\n"
            "백그라운드 작업 목록이 표시되는 자리입니다."
        )
        assert _consume_footer_sidecar(content) == content

    def test_midbody_footer_shaped_block_with_prose_after_preserved(self, sidecar):
        # Footer-shaped lines mid-body with real prose after them: not at the
        # message tail -> preserved (tail anchor).
        sidecar("")
        content = (
            "예시 풋터:\n"
            "[라이브]\n"
            "- 예시 작업\n"
            "위와 같은 모양입니다. 이 설명은 남아야 합니다."
        )
        assert _consume_footer_sidecar(content) == content

    def test_old_regression_shape_no_mass_deletion(self, sidecar):
        # The exact failure class: marker mid-sentence, long tail after it.
        sidecar("")
        tail = "이 뒤로 아주 긴 안내가 이어집니다. " * 20
        content = "현재 [라이브] 대시보드에서 확인 가능합니다. " + tail
        assert _consume_footer_sidecar(content) == content.rstrip()


# ---------------------------------------------------------------------------
# (b) a real trailing canonical footer block is still stripped
# ---------------------------------------------------------------------------

class TestTrailingFooterStripped:
    def test_trailing_multiline_block_stripped(self, sidecar):
        sidecar("")
        content = "작업 보고입니다.\n결과는 정상입니다.\n" + CANONICAL_FOOTER
        assert _consume_footer_sidecar(content) == (
            "작업 보고입니다.\n결과는 정상입니다."
        )

    def test_trailing_block_with_none_bullet_stripped(self, sidecar):
        sidecar("")
        content = "보고 끝.\n[라이브]\n- 없음\n[결정대기]\n- dec-102 문구 확정"
        assert _consume_footer_sidecar(content) == "보고 끝."

    def test_legacy_oneline_footer_stripped(self, sidecar):
        # Rev-3 one-line form: marker header line carrying trailing text.
        sidecar("")
        content = (
            "완료했습니다.\n"
            "[라이브] 배포 동생, 문서 동생 (진행중 2개) / [결정대기] 1건: dec-103"
        )
        assert _consume_footer_sidecar(content) == "완료했습니다."

    def test_trailing_blocks_with_blank_gap_stripped(self, sidecar):
        sidecar("")
        content = "본문.\n[라이브]\n- 작업 A\n\n[결정대기]\n- dec-104 승인"
        assert _consume_footer_sidecar(content) == "본문."

    def test_canonical_footer_appended_once_after_strip(self, sidecar):
        sidecar(CANONICAL_FOOTER)
        content = "본문 보고.\n" + CANONICAL_FOOTER
        result = _consume_footer_sidecar(content)
        assert result == "본문 보고.\n" + CANONICAL_FOOTER
        assert result.count("[라이브]") == 1

    def test_footer_only_message_returns_original(self, sidecar):
        # Stripping the whole message would yield empty; the fail-safe keeps
        # the original content (pre-existing contract).
        sidecar("")
        assert _consume_footer_sidecar(CANONICAL_FOOTER) == CANONICAL_FOOTER

    def test_sidecar_cleared_after_consume(self, sidecar):
        path = sidecar(CANONICAL_FOOTER)
        _consume_footer_sidecar("본문.")
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "footer": "", "ts": 0,
        }

    def test_sidecar_absent_content_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            sdk_bridge, "_FOOTER_SIDECAR", tmp_path / "missing.json")
        content = "본문 [라이브] 언급 포함.\n" + CANONICAL_FOOTER
        assert _consume_footer_sidecar(content) == content


# ---------------------------------------------------------------------------
# Regex-level adversarial cases (self-grill)
# ---------------------------------------------------------------------------

class TestRegexAdversarial:
    def test_marker_never_matches_mid_line(self):
        assert _FOOTER_BLOCK_RE.search("끝 문장 [라이브]") is None

    def test_double_bracket_options_never_matches(self):
        assert _FOOTER_BLOCK_RE.search("본문\n[[OPTIONS]]") is None

    def test_block_above_options_marker_not_stripped(self):
        # A footer-shaped run followed by [[OPTIONS]] is not at the tail;
        # preservation (no partial strip) is the safe direction.
        text = "본문\n[라이브]\n- 작업\n[[OPTIONS]]"
        assert _FOOTER_BLOCK_RE.search(text) is None

    def test_only_tail_block_stripped_when_earlier_block_exists(self):
        text = "[라이브]\n- 예시\n중간 설명 문장.\n[결정대기]\n- dec-105 승인"
        assert _FOOTER_BLOCK_RE.sub("", text).rstrip() == (
            "[라이브]\n- 예시\n중간 설명 문장."
        )

    def test_trailing_whitespace_tolerated(self):
        text = "본문.\n[라이브]\n- 작업 A\n\n"
        assert _FOOTER_BLOCK_RE.sub("", text).rstrip() == "본문."

    def test_bullet_containing_marker_literal_inside_block(self):
        text = "본문.\n[라이브]\n- [라이브] 관련 정리 작업"
        assert _FOOTER_BLOCK_RE.sub("", text).rstrip() == "본문."

    def test_long_bullet_run_no_blowup(self):
        # Pathological backtracking guard: long bullet run ending in prose
        # (no match) must return quickly and match nothing.
        text = "[라이브]\n" + "- 항목\n" * 500 + "마지막 줄은 산문입니다."
        assert _FOOTER_BLOCK_RE.search(text) is None
