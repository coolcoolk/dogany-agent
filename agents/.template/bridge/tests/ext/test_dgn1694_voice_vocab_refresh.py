"""DGN-1694 step 2: estate side of the derived voice vocabulary -- the boot
hook that refreshes the cache, and the reader/bridge cache lockstep."""
import logging
from pathlib import Path
import subprocess
import sys

import pytest

from bridge import config as config_mod
from bridge.ext.dogany_estate import voice_vocab as vocab_hook
from bridge.voice import load_derived_vocabulary


TEMPLATE = Path(__file__).resolve().parents[3]
READER = TEMPLATE / 'routines' / 'lib' / 'voice_vocab.py'


def test_real_reader_cache_round_trips_into_bridge(tmp_path):
    if not READER.is_file():
        pytest.skip('reader ships beside the bridge only in a full template tree')
    root = tmp_path / 'inst'
    (root / '.telegram_bot').mkdir(parents=True)
    subprocess.run([sys.executable, str(READER), str(root), '--write'], check=True,
                   capture_output=True, env={'HOME': str(tmp_path / 'home'), 'PATH': ''})
    terms = load_derived_vocabulary(root / '.telegram_bot' / 'voice-vocab.json')
    assert terms[0] == '\ub3c4\uac00\ub2c8' and 'Opus' in terms


def test_boot_hook_runs_reader_in_background(tmp_path, monkeypatch, caplog):
    root = tmp_path / 'inst'
    reader = root / 'routines' / 'lib' / 'voice_vocab.py'
    reader.parent.mkdir(parents=True)
    reader.write_text(
        'import pathlib, sys\n'
        'pathlib.Path(sys.argv[1], "called").write_text(" ".join(sys.argv[1:]))\n'
        'print("0\\tframework\\tone")\nprint("0\\tmodel\\ttwo")\n')
    monkeypatch.setattr(config_mod, 'PROJECT_ROOT', root)
    with caplog.at_level(logging.INFO, logger=vocab_hook.__name__):
        vocab_hook.refresh_voice_vocab().join(30)
    assert (root / 'called').read_text() == '%s --write' % root
    assert 'refreshed: 2 terms' in caplog.text


def test_boot_hook_absorbs_absent_and_failing_reader(tmp_path, monkeypatch, caplog):
    root = tmp_path / 'inst'
    root.mkdir()
    monkeypatch.setattr(config_mod, 'PROJECT_ROOT', root)
    with caplog.at_level(logging.INFO, logger=vocab_hook.__name__):
        vocab_hook.refresh_voice_vocab().join(30)
        reader = root / 'routines' / 'lib' / 'voice_vocab.py'
        reader.parent.mkdir(parents=True)
        reader.write_text('import sys\nsys.exit("broken")\n')
        vocab_hook.refresh_voice_vocab().join(30)
    assert 'reader absent' in caplog.text
    assert 'refresh failed (rc=1): broken' in caplog.text


def test_estate_extension_registers_the_hook():
    from bridge import ext
    ext._reset_for_tests()
    try:
        assert vocab_hook.refresh_voice_vocab in ext.boot_hooks()
    finally:
        ext._reset_for_tests()
