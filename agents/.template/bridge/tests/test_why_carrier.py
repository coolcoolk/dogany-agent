# test-component: live Why carrier and Telegram receipt custody
# test-tier: 0
# test-prereq: python3, tmp-files, subprocess
# test-new-component: real bot handler through SDK hooks to core and fake Telegram
# test-covers: agents/.template/bridge/why_carrier.py agents/.template/bridge/bot.py agents/.template/bridge/sdk_bridge.py agents/.template/routines/lib/why_net/runtime.py agents/.template/routines/lib/why_net/ups.py agents/.template/routines/lib/why_net/runtime_intake.py agents/.template/routines/lib/why_net/evidence.py
"""No live Telegram/model calls. Only model reasoning and transport are fake."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
import bridge.bot as bot_module
import bridge.sdk_bridge as sdk
from bridge import why_carrier

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'routines/lib'))
from why_net import runtime, store
from why_net.intake import digest
from why_net.records import TicketlessRecords
from term_registry import schema_labels


class Transport:
    id = 999

    def __init__(self):
        self.sent = []
        self.before_ack = None
        self.fail_send = False

    async def send_message(self, **kw):
        if self.fail_send:
            raise OSError('uncertain transport')
        if self.before_ack:
            self.before_ack()
            self.before_ack = None
        self.sent.append(kw)
        return SimpleNamespace(message_id=100 + len(self.sent), chat_id=42)

    send_chat_action = AsyncMock()
    edit_message_text = AsyncMock(return_value=SimpleNamespace(message_id=102, chat_id=42))


class CarrierE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ('DOGANY_DISPATCH_WORKER', 'DOGANY_CODEX_ADAPTER_ROOT',
                    'DOGANY_ROOT', 'DOGANY_AGENT_CONF', 'DOGANY_BOT_DATA_DIR'):
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory(prefix='why-carrier-', dir=os.environ.get('DOGANY_RUN_SCRATCH'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config/i18n').mkdir(parents=True)
        (self.root / 'config/i18n/terms.json').write_bytes(
            (ROOT / 'agents/.template/config/i18n/terms.json').read_bytes())
        (self.root / 'config/agent.conf').write_text('WHY_NET=on\n')
        (self.root / 'config/why-runtime.json').write_text(json.dumps(
            dict(owner_id='owner-a', runtime='bridge', chat_id=42)))
        (self.root / 'routines').symlink_to(ROOT / 'routines')
        self.transport = Transport()
        self.bridge = sdk.SdkBridge()
        self.bridge.project_root = self.root
        self.bot = bot_module.TelegramBot.__new__(bot_module.TelegramBot)
        self.bot.application = SimpleNamespace(bot=self.transport)
        self.bot._runtime_active_sessions = set()
        self.bot._bot_of = lambda message: self.transport
        self.bot._get_real_model = lambda session: 'm'
        self.bot._with_outbound_context = lambda text, *args: text
        self.bot._effective_session_id = lambda *args: 's1'
        self.bot._permission_callback = AsyncMock()
        self.bot._proactive_push = AsyncMock()
        self.bot._settle_turn = AsyncMock(side_effect=lambda **kw: kw['response'])
        self.bot._reply_smart = AsyncMock()
        self.draft = dict(unit=dict(work_key='monitor', scope_revision=1,
            scope_digest='observe-stalls', targets=['skill/monitor']),
            symptom='The monitor misses stalls.', problem='Stalls go unobserved.',
            why='Catch stalls before work is lost.', direction='Observe and report stalls.')
        self.revision = 0
        self.approval = ''
        self.decisions = []
        self.writer_results = []
        outer = self

        class Client:
            def __init__(self, options):
                self.options, self.q = options, asyncio.Queue()
                self.connect = AsyncMock()
                self.disconnect = AsyncMock()

            async def hook(self, kind, data):
                answers = []
                for matcher in self.options.hooks.get(kind, []):
                    if matcher.matcher not in (None, '*'):
                        continue
                    for callback in matcher.hooks:
                        answers.append(await callback(data, None, None))
                return answers

            async def query(self, prompt, session_id=None):
                data = dict(session_id='s1', prompt=prompt, cwd=str(outer.root))
                assert not self.options.hooks.get('UserPromptSubmit')
                assert not self.options.hooks.get('Stop')
                before = await self.hook('PreToolUse', dict(data,
                    tool_name='Bash', tool_input={'command': 'echo hello'}))
                outer.decisions.append(before)
                body = 'All requested work is available. This answer uses the generated English words and must reach the owner without language filtering.'
                await self.q.put(AssistantMessage(
                    content=[TextBlock(text=body)], model='m'))
                await self.q.put(ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id='s1', result=body))

            async def receive_messages(self):
                while True:
                    yield await self.q.get()

        session = SimpleNamespace(get_session=AsyncMock(return_value=dict(session_id='s1', model='m')),
            should_start_new_session=AsyncMock(return_value=False), update_session=AsyncMock(),
            set_last_user_message_at=AsyncMock(), record_context_fill=AsyncMock())
        for target, name, value in (
            (sdk, 'PROJECT_ROOT', self.root), (bot_module, 'PROJECT_ROOT', self.root),
            (sdk, 'ClaudeSDKClient', Client), (sdk, 'session_manager', session),
            (bot_module, 'session_manager', session), (bot_module, 'sdk_bridge', self.bridge),
            (sdk, '_compose_system_prompt', lambda: 'Test model'),
            (sdk.config, 'locale', 'ko'),
            (sdk, 'resolve_claude_cli', lambda: None),
            (sdk.SdkBridge, '_maybe_mark_options', AsyncMock(side_effect=lambda _previous, body, **kw: (body, False))),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        for state in self.bridge._streams.values():
            for task in (state.reader_task, state.typing_task):
                if task:
                    task.cancel()
            await asyncio.gather(state.reader_task, state.typing_task, return_exceptions=True)

    def message(self, text, mid, reply_to=None):
        reply = SimpleNamespace(message_id=reply_to, text='readback',
            from_user=SimpleNamespace(id=999, is_bot=True)) if reply_to else None
        return SimpleNamespace(text=text, chat_id=42, message_id=mid, date=datetime.now(timezone.utc),
            chat=SimpleNamespace(id=42, send_action=AsyncMock()), reply_to_message=reply,
            reply_text=AsyncMock())

    async def turn(self, message, capture=True):
        if capture:
            why_carrier.capture(self.bot, message)
        update = SimpleNamespace(message=message, effective_chat=message.chat, effective_user=SimpleNamespace(id=42))
        await self.bot._process_user_message_text(update, 42, message.text)

    async def test_missing_why_evidence_delivers_the_complete_answer(self):
        (self.root / 'config/why-runtime.json').unlink()
        with patch.object(why_carrier, 'publish_turn', side_effect=FileNotFoundError), \
             patch.object(why_carrier, 'publish_tool', side_effect=FileNotFoundError), \
             patch.object(why_carrier, 'deliver_readback', side_effect=OSError):
            await self.turn(self.message('echo hello', 1))
        self.assertEqual([[]], self.decisions)
        self.bot._reply_smart.assert_awaited_once()
        self.assertEqual('All requested work is available. This answer uses the generated English words and must reach the owner without language filtering.',
                         self.bot._reply_smart.await_args.args[1])

    async def test_why_off_request_is_delivered_without_publication(self):
        await self.turn(self.message('Turn Why off', 2))
        self.bot._reply_smart.assert_awaited_once()
        self.assertEqual([[]], self.decisions)

    async def test_why_off_preserves_ordinary_chat(self):
        (self.root / 'config/agent.conf').write_text('WHY_NET=off\n')
        await self.test_why_off_request_is_delivered_without_publication()

    async def test_stored_why_data_is_not_removed_by_ordinary_chat(self):
        path = self.root / '.telegram_bot/state/why-net/old.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"historic": true}')
        await self.turn(self.message('hello', 3))
        self.assertEqual({'historic': True}, json.loads(path.read_text()))
        self.bot._reply_smart.assert_awaited_once()
