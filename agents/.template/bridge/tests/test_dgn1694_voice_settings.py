"""Machine defaults, instance overrides and installed transcription APIs."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bridge.voice import LocalWhisperTranscriber


BRIDGE_PARENT = Path(__file__).resolve().parents[2]


def settings(tmp_path, machine='', instance='', inherited=None):
    home = tmp_path / 'home'
    # ESTATE:BEGIN machine voice defaults live under the estate machine root (config.MACHINE_VOICE_PATH)
    (home / '.dogany').mkdir(parents=True)
    (home / '.dogany' / 'voice.conf').write_text(machine)
    # ESTATE:ELSE
    # home.mkdir(parents=True)
    # ESTATE:END
    root = tmp_path / 'instance'
    (root / '.telegram_bot').mkdir(parents=True)
    (root / '.telegram_bot' / '.env').write_text(
        'TELEGRAM_BOT_TOKEN=test:token\n' + instance
    )
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'TMPDIR', 'SYSTEMROOT')}
    env.update(HOME=str(home), PROJECT_ROOT=str(root), PYTHONPATH=str(BRIDGE_PARENT))
    env.update(inherited or {})
    result = subprocess.run([
        sys.executable, '-c',
        'from bridge.config import config; print(config.model_dump_json())',
    ], env=env, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_machine_defaults_and_instance_override(tmp_path):
    value = settings(tmp_path, 'model=large-v3-turbo\nlanguage=ja\n',
                     'LOCAL_WHISPER_MODEL=small\nWHISPER_LANGUAGE=fr\n',
                     {'LOCAL_WHISPER_MODEL': 'medium'})
    assert value['local_whisper_model'] == 'small'
    assert value['whisper_language'] == 'fr'


# ESTATE:BEGIN reads the estate machine voice.conf; the public MACHINE_VOICE_PATH is None
def test_machine_defaults(tmp_path):
    value = settings(tmp_path, 'model=large-v3-turbo\nlanguage=ja\n', 'LOCALE=ko\n')
    assert value['local_whisper_model'] == 'large-v3-turbo'
    assert value['whisper_language'] == 'ja'
# ESTATE:END


@pytest.mark.parametrize('instance, expected', [
    ('', 'en'), ('LOCALE=ko\n', 'ko'), ('AGENT_LANG=ja-JP\n', 'ja'),
    ('LOCALE=fr_FR\nAGENT_LANG=ko\n', 'fr'),
    ('LOCALE=ko\nWHISPER_LANGUAGE=auto\n', None),
    ('LOCALE=ko\nWHISPER_LANGUAGE=\n', None),
])
def test_language_derivation(tmp_path, instance, expected):
    assert settings(tmp_path, instance=instance)['whisper_language'] == expected


# ESTATE:BEGIN reads the estate machine voice.conf; the public MACHINE_VOICE_PATH is None
def test_vocabulary_merge(tmp_path):
    value = settings(tmp_path, 'vocabulary="alpha, multi word, alpha"\n',
                     'WHISPER_VOCABULARY="multi word, beta"\n')
    assert value['whisper_vocabulary'] == ['alpha', 'multi word', 'beta']
# ESTATE:END


@pytest.mark.parametrize('modern', [True, False])
def test_installed_signature_selects_hint(modern):
    received = {}

    def recent(audio, *, hotwords=None, **kwargs):
        received.update(hotwords=hotwords, **kwargs)
        return iter([SimpleNamespace(text='transcript')]), None

    def old(audio, *, initial_prompt=None, **kwargs):
        received.update(initial_prompt=initial_prompt, **kwargs)
        return iter([SimpleNamespace(text='transcript')]), None

    transcriber = LocalWhisperTranscriber(language='en', vocabulary=['alpha', 'multi word'])
    transcriber._model = SimpleNamespace(transcribe=recent if modern else old)
    assert transcriber._run(Path('clip.wav')) == 'transcript'
    assert received == {
        ('hotwords' if modern else 'initial_prompt'): 'alpha, multi word',
        'language': 'en', 'beam_size': 5, 'vad_filter': True,
    }


def test_no_vocabulary_has_no_hint():
    received = {}

    def transcribe(audio, **kwargs):
        received.update(kwargs)
        return iter([SimpleNamespace(text='plain')]), None

    transcriber = LocalWhisperTranscriber()
    transcriber._model = SimpleNamespace(transcribe=transcribe)
    assert transcriber._run(Path('clip.wav')) == 'plain'
    assert 'hotwords' not in received and 'initial_prompt' not in received


def test_runtime_dependency_is_required():
    requirements = (BRIDGE_PARENT / 'bridge' / 'requirements.txt').read_text()
    assert 'faster-whisper' in requirements.split('# --- optional')[0].splitlines()
