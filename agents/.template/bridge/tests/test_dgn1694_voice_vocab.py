"""DGN-1694 step 2: derived vocabulary cache consumption and budget cut."""
import json
from pathlib import Path
from types import SimpleNamespace

from bridge import voice
from bridge.voice import LocalWhisperTranscriber, fit_vocabulary, load_derived_vocabulary


def chars(text):
    return len(text)


def test_fit_keeps_priority_order_dedupes_and_cuts():
    phrases = ['alpha', 'Beta', 'ALPHA', ' ', 'a-very-long-phrase', 'gamma', 'delta']
    # " alpha, Beta, gamma" = 19 chars; adding ", delta" would make 26.
    assert fit_vocabulary(phrases, chars, 19) == ['alpha', 'Beta', 'gamma']
    assert fit_vocabulary(phrases, chars, 1000) == [
        'alpha', 'Beta', 'a-very-long-phrase', 'gamma', 'delta']
    assert fit_vocabulary(phrases, chars, 3) == []
    assert fit_vocabulary([], chars, 223) == []


class FakeTokenizer:
    """One token per character; records calls."""

    def __init__(self):
        self.calls = 0

    def encode(self, text, add_special_tokens=True):
        assert add_special_tokens is False
        self.calls += 1
        return SimpleNamespace(ids=list(text))


def fake_model(received, *, modern=True, max_length=24, tokenizer=None):
    def recent(audio, *, hotwords=None, **kwargs):
        received.append(hotwords)
        return iter([SimpleNamespace(text='t')]), None

    def old(audio, *, initial_prompt=None, **kwargs):
        received.append(initial_prompt)
        return iter([SimpleNamespace(text='t')]), None

    return SimpleNamespace(transcribe=recent if modern else old, max_length=max_length,
                           hf_tokenizer=tokenizer)


def test_run_cuts_with_model_tokenizer_once_for_both_hint_kinds():
    for modern in (True, False):
        received, tokenizer = [], FakeTokenizer()
        transcriber = LocalWhisperTranscriber(
            vocabulary=['alpha', 'beta', 'gamma', 'delta'])
        # max_length 24 -> budget 11 tokens: " alpha, beta" = 12 -> only alpha.
        transcriber._model = fake_model(received, modern=modern, tokenizer=tokenizer)
        transcriber._run(Path('a.wav'))
        calls = tokenizer.calls
        transcriber._run(Path('b.wav'))
        assert received == ['alpha', 'alpha']
        assert tokenizer.calls == calls  # the cut is computed once, not per message


def test_run_without_tokenizer_uses_utf8_bytes():
    received = []
    hangul = '\ub3c4\uac00\ub2c8'  # 3 syllables = 9 UTF-8 bytes
    transcriber = LocalWhisperTranscriber(vocabulary=[hangul, 'x'])
    # max_length 22 -> budget 10: " " + 9 bytes fits; ", x" does not.
    transcriber._model = fake_model(received, max_length=22)
    transcriber._run(Path('a.wav'))
    assert received == [hangul]


def test_default_budget_is_whisper_prompt_limit():
    received = []
    words = ['w%03d' % i for i in range(200)]
    transcriber = LocalWhisperTranscriber(vocabulary=words)
    transcriber._model = SimpleNamespace(
        transcribe=fake_model(received).transcribe, hf_tokenizer=FakeTokenizer())
    transcriber._run(Path('a.wav'))
    assert len(' ' + received[0]) <= 223 < len(' ' + received[0]) + len(', w999')


def write_cache(path, body):
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding='utf-8')
    return path


def test_load_derived_vocabulary(tmp_path):
    assert load_derived_vocabulary(tmp_path / 'missing.json') == []
    assert load_derived_vocabulary(write_cache(tmp_path / 'a', 'garbage')) == []
    assert load_derived_vocabulary(write_cache(tmp_path / 'b', {'schema': 2, 'terms': []})) == []
    assert load_derived_vocabulary(write_cache(tmp_path / 'c', [1, 2])) == []
    good = {'schema': 1, 'terms': [{'term': 'one'}, {'term': 2}, 'x', {'term': 'two'}]}
    assert load_derived_vocabulary(write_cache(tmp_path / 'd', good)) == ['one', 'two']


def test_build_transcriber_puts_explicit_vocabulary_first(tmp_path, monkeypatch):
    cache = write_cache(tmp_path / 'voice-vocab.json',
                        {'schema': 1, 'terms': [{'term': 'derived'}, {'term': 'explicit'}]})
    monkeypatch.setattr(voice, 'VOCAB_CACHE_PATH', cache)
    monkeypatch.setattr(voice.config, 'whisper_vocabulary', ['explicit', 'machine'])
    transcriber = voice.build_transcriber()
    assert transcriber.vocabulary == ['explicit', 'machine', 'derived', 'explicit']
    received = []
    transcriber._model = fake_model(received, max_length=448, tokenizer=FakeTokenizer())
    transcriber._run(Path('a.wav'))
    assert received == ['explicit, machine, derived']
