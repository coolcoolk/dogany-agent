"""DGN-1838: a substantive answer written mid-turn must not be folded away.

Measured 2026-10-03 09:29-09:32 KST (a dev agent transcript, live stream):
the owner asked for the post-opener flow. The model ran 5 tools, wrote the
9-step answer, a Stop hook blocked once, and the model wrote a 403-char
supplement. Every live AssistantMessage carries stop_reason=None (DGN-1703),
so in fold mode BOTH texts were captured as interim and only the LAST message
became the final body: the answer sat inside the collapsed progress quote and
the final bubble carried only the supplement.

Fix under test: at finalize, an interim block that reads as an answer
(formatting.is_substantive_interim) and is not restated by the final
(formatting.interim_restated) leaves the fold and is delivered as its own
normal message right before the final answer -- exactly once, in turn order.
Short progress lines keep folding.
"""

import asyncio
import unittest
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

import bridge.bot as botmod
from bridge.formatting import (
    FOLD_CAPTION_NORMAL,
    PROMOTE_LONG_CHARS,
    interim_restated,
    is_substantive_interim,
    split_promoted_interim,
)
from bridge.sdk_bridge import SdkBridge, _UserStreamState
from bridge.streaming import StreamingMessageHandler
from bridge.tests.test_dgn1651_stop_block_duplicate import (
    _Screen,
    _asst,
    _pending,
    _result,
)

# The real case, verbatim (09:32:23 interim answer, 09:32:37 supplement).
ANSWER = """첫 인사 수정을 합쳤습니다. 에이전트 기본 안내 문서에도 같은 규칙을 넣었고, 관련 검사가 모두 통과했습니다. 지금 원격에 올리고 리허설 설치본을 다시 만드는 중입니다.

**첫 인사 뒤 흐름 (킷이 없는 메인 에이전트)**
1. **첫 인사:** 에이전트가 먼저 "새로 연결된 개인 에이전트"라고 짧게 소개하고 인사를 유도합니다. 할 일 예시나 질문은 없습니다.
2. **형님이 인사하시면:** 에이전트가 답하면서 자연스럽게 이름을 정해 달라고 합니다.
3. **이모지:** 정한 이름에 어울리는 이모지 3~4개를 버튼으로 보여 줍니다.
4. **호칭:** 형님을 뭐라고 부를지 묻습니다. 형님 이름이나 다른 에이전트가 쓰는 호칭이 있으면 버튼으로 보여 줍니다.
5. **말투:** 말투 후보를 버튼으로 보여 줍니다.
6. **유머:** 유머 정도를 10~90% 버튼으로 고릅니다.
7. **역할 질문은 없습니다:** 킷을 켤 때 그 킷이 정합니다.
8. **마무리:** 정한 내용을 정리해 보여 주고, 다음에 할 수 있는 것을 2~3개 버튼으로 제안합니다. 이번 수정으로 이 단계에서도 "오늘 일정 브리핑" 같은 생활 예시는 나오지 않습니다.
9. **라이프킷:** "생활관리 켜줘"라고 하시면 라이프킷을 설치합니다. 이때부터 라이프킷이 자기 첫 안내로 "어떤 것부터 해 보실래요?"를 묻고, 고른 것만 켭니다.

설치본이 준비되면 바로 알려 드리겠습니다. 첫 인사를 다시 보시려면 지금 에이전트를 지우고 새로 설치하시면 됩니다. 원하시면 그 정리도 제가 해 두겠습니다."""

SUPPLEMENT = """에이전트 기본 안내 문서 수정을 어디서 했는지 기록을 보충합니다.

이 수정은 백그라운드 작업이 아니라, 이 대화 안에서 skill 문서 전담 편집 동생(작업 ID ac5bb9562793402ab)이 했습니다. 그래서 작업 추적 기록에는 남지 않았습니다.
- **고친 것:** 에이전트 기본 안내 문서 한 파일에 세 가지를 넣었습니다.
  - 킷이 없을 때 첫 인사 규칙
  - 다음 단계 추천 메뉴에서 생활 예시 제거
  - "무엇이든 맡는다"는 역할 문장이 첫 인사 내용이 아니라는 연결 문장
- **검사:** 첫 대화 검사 6개, 설치 리허설 검사 293개, 첫 인사 검사 5개 모두 통과했습니다.
- **반영:** 제가 정본에 커밋했고(84358bf6), 지금 원격 올리기와 리허설 설치본 만들기가 진행 중입니다."""

ANSWER_NEEDLE = "형님이 인사하시면"
SUPPLEMENT_NEEDLE = "기록을 보충합니다"
FEEDBACK = (
    "Stop hook feedback:\nSelf-landed ANOMALY -- observable item(s) "
    "(rule L10, mechanism 8)"
)

# Second answer for the ordering cases: a different structured report.
REPORT = """세 갈래 모두 동시에 돌고 있습니다.

- **설정 보존 작업 합치기:** 별도 작업 폴더에서 합쳤고 충돌은 없었습니다. 지금 그 폴더에서 핵심 검사를 돌리고 있습니다.
- **스킬 설명서 문안:** 동생이 만들고 있습니다. 나오면 형님께 확인받겠습니다.
- **동료 에이전트 알림:** 보냈습니다. 다음 헬스 팩 발행 전에 고칠 한 줄과 용어집 반영 몫을 담았습니다.

dev.68이 나오면 이 작업을 바로 이어 붙여서 다음 dev 버전에 넣겠습니다."""
REPORT_NEEDLE = "세 갈래 모두"


def _text(text: str):
    # Live shape: every streamed message has stop_reason=None (DGN-1703).
    return _asst(None, [TextBlock(text=text)])


def _tool(tid: str, name: str = "Bash"):
    return _asst(None, [ToolUseBlock(id=tid, name=name, input={"command": "x"})])


def _tool_result(tid: str):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="ok")])


def _tools(*tids: str) -> list:
    out = []
    for tid in tids:
        out += [_tool(tid), _tool_result(tid)]
    return out


def _feedback():
    return UserMessage(content=[TextBlock(text=FEEDBACK)])


def run_turn(messages_seq, interim_mode: str = "fold", push_fails: bool = False):
    """Reader loop over a REAL handler + the REAL bot delivery seat, with the
    proactive delivery seat wired to the same screen.

    Returns (response, pushes, view, screen): pushes are the bodies handed to
    the proactive seat in call order; view is the owner's chat at turn end.
    """

    async def _inner():
        screen = _Screen()
        bot = screen.bot()
        handler = StreamingMessageHandler(bot, chat_id=1, user_id=1)
        handler.min_chars = 1
        handler.min_interval = 0.0

        bridge_obj = SdkBridge()
        req = _pending(handler)
        req.sent = True
        state = _UserStreamState(client=MagicMock(), model=None)
        state.pending.append(req)
        state.last_chat_id = 1
        pushes: List[tuple] = []

        async def _push(chat_id, content, has_options, classifier_injected):
            if push_fails:
                raise RuntimeError("telegram down")
            pushes.append((content, has_options))
            screen._next += 1
            screen.texts[screen._next] = content
            screen.order.append(screen._next)
            screen.log.append(("push", screen._next))

        state.proactive_push = _push

        async def fake_receive():
            for m in messages_seq:
                yield m

        state.client.receive_messages = fake_receive
        bridge_obj._streams[1] = state

        async def _no_classifier(user_message, content):
            return content, False

        bridge_obj._maybe_mark_options = _no_classifier

        with patch(
            "bridge.sdk_bridge.STREAM_INTERIM", interim_mode == "inline"
        ), patch("bridge.sdk_bridge.INTERIM_MODE", interim_mode):
            await bridge_obj._reader_loop(1, state)

        response = await asyncio.wait_for(req.future, timeout=1.0)

        message = MagicMock()
        message.get_bot.return_value = bot
        message.chat.id = 1

        async def _reply_text(text, **kwargs):
            screen._next += 1
            screen.texts[screen._next] = text
            screen.order.append(screen._next)
            screen.log.append(("send", screen._next))

        message.reply_text = AsyncMock(side_effect=_reply_text)

        instance = object.__new__(botmod.TelegramBot)
        if response.content:
            with patch.object(
                botmod.TelegramBot, "_reply_link_id", return_value=None
            ), patch.object(
                botmod.TelegramBot, "_send_content_artifacts", new=AsyncMock()
            ):
                await botmod.TelegramBot._reply_smart(
                    instance,
                    message,
                    response.content,
                    force_options=response.has_options,
                    streamed=response.streamed,
                    draft_message_ids=response.draft_message_ids,
                    assembled=getattr(response, "turn_assembled", False),
                )
        return response, [p[0] for p in pushes], screen.view(), screen

    return asyncio.run(_inner())


def _is_fold(text: str) -> bool:
    return FOLD_CAPTION_NORMAL in text or "<blockquote" in text


def _normal(view: List[str]) -> List[str]:
    return [t for t in view if not _is_fold(t)]


def _index(view: List[str], needle: str) -> int:
    hits = [i for i, t in enumerate(view) if needle in t and not _is_fold(t)]
    assert len(hits) == 1, (needle, view)
    return hits[0]


def real_case(interim_mode: str = "fold", **kw):
    """The 09:29 turn, message for message (tools, answer, Stop block,
    supplement)."""
    return run_turn(
        [
            *_tools("t1", "t2", "t3", "t4", "t5"),
            _text(ANSWER),
            _feedback(),
            _text(SUPPLEMENT),
            _result(result=SUPPLEMENT),
        ],
        interim_mode=interim_mode,
        **kw,
    )


class TestRealCaseReplay(unittest.TestCase):
    def test_pre_fix_shape_buries_the_answer(self):
        # Pins the defect: with promotion disabled the answer exists only
        # inside the collapsed fold, and the final bubble is the supplement.
        with patch(
            "bridge.sdk_bridge.split_promoted_interim",
            side_effect=lambda blocks, final: ([], list(blocks)),
        ):
            response, pushes, view, _screen = real_case()
        self.assertEqual(pushes, [])
        self.assertNotIn(ANSWER_NEEDLE, "\n".join(_normal(view)))
        self.assertTrue(any(_is_fold(t) and ANSWER_NEEDLE in t for t in view))
        self.assertEqual(response.content.strip(), SUPPLEMENT.strip())

    def test_answer_reaches_owner_as_normal_bubble(self):
        response, pushes, view, _screen = real_case()
        self.assertEqual(pushes, [ANSWER.strip()])
        # Exactly once on the whole screen, as a normal (non-fold) bubble.
        self.assertEqual(sum(t.count(ANSWER_NEEDLE) for t in view), 1)
        # In order: the answer bubble stands above the final supplement.
        self.assertLess(_index(view, ANSWER_NEEDLE), _index(view, SUPPLEMENT_NEEDLE))
        # The final body is the supplement, untouched.
        self.assertEqual(response.content.strip(), SUPPLEMENT.strip())
        # The fold held only the answer (the supplement is final-overlap), so
        # the bubble is gone rather than collapsed around nothing.
        self.assertFalse(any(_is_fold(t) for t in view))

    def test_numbered_answer_list_stays_body_text(self):
        # The proactive seat is told has_options=False: the 9 steps are an
        # answer, never a button menu.
        captured = []

        async def _inner_push(chat_id, content, has_options, ci):
            captured.append(has_options)

        bridge_obj = SdkBridge()
        state = _UserStreamState(client=MagicMock(), model=None)
        state.proactive_push = _inner_push

        async def _go():
            req = _pending(None)
            return await bridge_obj._promote_interim_answers(
                1, state, req, [ANSWER], False
            )

        self.assertEqual(asyncio.run(_go()), [])
        self.assertEqual(captured, [False])


class TestShortNarrationStillFolds(unittest.TestCase):
    def test_progress_lines_stay_in_the_fold(self):
        final = "확인했습니다. 설정은 그대로 두면 됩니다."
        response, pushes, view, _screen = run_turn(
            [
                _text("파일을 확인할게요."),
                *_tools("t1"),
                _text("테스트를 돌리는 중입니다. 결과를 보고 판단하겠습니다."),
                *_tools("t2"),
                _text(final),
                _result(result=final),
            ]
        )
        self.assertEqual(pushes, [])
        folds = [t for t in view if _is_fold(t)]
        self.assertEqual(len(folds), 1)
        self.assertIn("파일을 확인할게요", folds[0])
        self.assertEqual(response.content.strip(), final)

    def test_long_single_paragraph_narration_stays_folded(self):
        # Measured: plain one-paragraph blocks of 200-599 chars are worker
        # narration, not answers.
        line = (
            "Confirmed on dev: the hook writer still targets settings.json and "
            "the local file appears 0 times in the installer, so candidate one "
            "has not landed yet; checking the dispatch log next before I write "
            "anything, then I will compare the two branches side by side."
        )
        self.assertGreaterEqual(len(line), 200)
        self.assertFalse(is_substantive_interim(line))
        response, pushes, _view, _screen = run_turn(
            [_text(line), *_tools("t1"), _text("끝났습니다."), _result(result="")]
        )
        self.assertEqual(pushes, [])


class TestDedupeWhenRestated(unittest.TestCase):
    def test_final_restating_the_answer_delivers_it_once(self):
        # The final re-writes the answer (emphasis dropped, intro/outro added):
        # no separate promoted bubble -- the final carries it.
        restated = (
            "정리해 드립니다.\n\n"
            + ANSWER.replace("**", "")
            + "\n\n원격 올리기는 끝났습니다."
        )
        response, pushes, view, _screen = run_turn(
            [
                _text(ANSWER),
                *_tools("t1", "t2"),
                _text(restated),
                _result(result=restated),
            ]
        )
        self.assertEqual(pushes, [])
        self.assertEqual(
            sum(t.count(ANSWER_NEEDLE) for t in _normal(view)), 1
        )
        self.assertIn("정리해 드립니다", response.content)

    def test_verbatim_restatement_leaves_no_fold(self):
        response, pushes, view, _screen = run_turn(
            [_text(ANSWER), *_tools("t1"), _text(ANSWER), _result(result=ANSWER)]
        )
        self.assertEqual(pushes, [])
        self.assertEqual(sum(t.count(ANSWER_NEEDLE) for t in view), 1)

    def test_mid_turn_rewrite_promotes_only_the_later_version(self):
        rewritten = ANSWER.replace("10~90%", "10~90% 사이에서")
        response, pushes, view, _screen = run_turn(
            [
                _text(ANSWER),
                *_tools("t1"),
                _text(rewritten),
                *_tools("t2"),
                _text("설치본 준비됐습니다."),
                _result(result=""),
            ]
        )
        self.assertEqual(pushes, [rewritten.strip()])
        self.assertEqual(
            sum(t.count(ANSWER_NEEDLE) for t in _normal(view)), 1
        )


class TestOrdering(unittest.TestCase):
    def test_two_answers_and_narration_land_in_turn_order(self):
        final = "다 끝났습니다."
        response, pushes, view, screen = run_turn(
            [
                _text("먼저 상태를 봅니다."),
                *_tools("t1"),
                _text(ANSWER),
                *_tools("t2"),
                _text("이어서 나머지를 확인합니다."),
                *_tools("t3"),
                _text(REPORT),
                *_tools("t4"),
                _text(final),
                _result(result=final),
            ]
        )
        self.assertEqual(pushes, [ANSWER.strip(), REPORT.strip()])
        # [fold: progress lines] -> answer -> report -> final
        self.assertTrue(_is_fold(view[0]))
        self.assertIn("먼저 상태를 봅니다", view[0])
        self.assertNotIn(ANSWER_NEEDLE, view[0])
        self.assertNotIn(REPORT_NEEDLE, view[0])
        a = _index(view, ANSWER_NEEDLE)
        b = _index(view, REPORT_NEEDLE)
        f = _index(view, final)
        self.assertLess(a, b)
        self.assertLess(b, f)

    def test_empty_final_after_answer_still_delivers_it(self):
        response, pushes, view, _screen = run_turn(
            [_text(ANSWER), *_tools("t1"), _result(result="")]
        )
        self.assertEqual(pushes, [ANSWER.strip()])
        self.assertEqual(response.content, "")
        self.assertEqual(sum(t.count(ANSWER_NEEDLE) for t in view), 1)

    def test_push_failure_rides_the_final_body_in_order(self):
        response, pushes, view, _screen = real_case(push_fails=True)
        self.assertEqual(pushes, [])
        body = response.content
        self.assertLess(body.index(ANSWER_NEEDLE), body.index(SUPPLEMENT_NEEDLE))
        self.assertEqual(sum(t.count(ANSWER_NEEDLE) for t in view), 1)

    def test_streamed_final_gets_the_answer_prepended(self):
        # stop_reason-bearing stream (synthetic shape): the final already
        # streamed into a draft, so a separate push would land BELOW it. The
        # answer is prepended instead and the draft is rewritten in place.
        final = "다 끝났습니다."
        response, pushes, view, _screen = run_turn(
            [
                _asst("tool_use", [TextBlock(text=ANSWER)]),
                _asst("tool_use", [ToolUseBlock(id="t1", name="Bash", input={})]),
                _tool_result("t1"),
                _asst("end_turn", [TextBlock(text=final)]),
                _result(result=final),
            ]
        )
        self.assertEqual(pushes, [])
        self.assertTrue(response.streamed)
        self.assertTrue(response.turn_assembled)
        normal = _normal(view)
        self.assertEqual(len(normal), 1)
        self.assertLess(normal[0].index(ANSWER_NEEDLE), normal[0].index(final))


class TestOtherModesUntouched(unittest.TestCase):
    def test_inline_and_suppress_never_promote(self):
        for mode in ("inline", "suppress"):
            with self.subTest(mode=mode):
                _response, pushes, _view, _screen = real_case(interim_mode=mode)
                self.assertEqual(pushes, [])


class TestClassifier(unittest.TestCase):
    def test_measured_shapes(self):
        self.assertTrue(is_substantive_interim(ANSWER))
        self.assertTrue(is_substantive_interim(SUPPLEMENT))
        self.assertTrue(is_substantive_interim(REPORT))
        self.assertFalse(is_substantive_interim("파일을 확인할게요."))
        # Bold lead line alone, short: a progress headline.
        self.assertFalse(
            is_substantive_interim("**찾았습니다. 오늘 논의한 핀이 실제로 있습니다.**")
        )
        three_paras = "가" * 80 + "\n\n" + "나" * 80 + "\n\n" + "다" * 60
        self.assertTrue(is_substantive_interim(three_paras))
        self.assertFalse(is_substantive_interim("x" * (PROMOTE_LONG_CHARS - 1)))
        self.assertTrue(is_substantive_interim("x" * PROMOTE_LONG_CHARS))

    def test_restated_similarity(self):
        self.assertTrue(interim_restated(ANSWER, ANSWER.replace("**", "")))
        self.assertTrue(interim_restated(ANSWER, "서론\n\n" + ANSWER + "\n\n끝"))
        self.assertFalse(interim_restated(ANSWER, SUPPLEMENT))
        self.assertFalse(interim_restated(ANSWER, ""))

    def test_split_keeps_order_and_never_raises(self):
        blocks = ["진행 중", ANSWER, "다음 단계", REPORT]
        promoted, kept = split_promoted_interim(blocks, "끝")
        self.assertEqual(promoted, [ANSWER, REPORT])
        self.assertEqual(kept, ["진행 중", "다음 단계"])
        self.assertEqual(split_promoted_interim(None, "x"), ([], []))


if __name__ == "__main__":
    unittest.main()
