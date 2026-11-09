# test-lane: fast
# test-component: memory-write-compression
# test-tier: 0
# test-prereq: tmp-files
# test-covers: agents/.template/memory-engine/memory.py
# test-new-component: Write compression -> Markdown/FTS has no existing suite; consolidation tests exercise a separate ingestion path.
"""DGN-2122: synthetic source evidence survives compression through storage.

Only the model transport and embeddings are stubbed. Real parsing, write,
redaction and SQLite/FTS indexing run against temporary files. No owner data.
"""
import argparse
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory  # noqa: E402


@pytest.fixture
def writer(tmp_path, monkeypatch):
    # Match the installed layout: the shared archive fixture is a sibling of
    # memories, never nested inside it (archive_engine's existing invariant).
    memories = tmp_path / "memories"
    memories.mkdir()
    target = memories / "inbox.md"
    target.write_text("<!-- dogany-format: section -->\n### Synthetic\n", encoding="utf-8")
    db = tmp_path / "state.db"
    monkeypatch.setattr(memory, "MEMORIES_DIR", str(memories))
    monkeypatch.setattr(memory, "DB_PATH", str(db))
    monkeypatch.setattr(memory, "embed", lambda text: [0.0] * memory.EMBED_DIM)
    calls = []
    responses = []

    def model_run(argv, **kwargs):
        assert argv[:4] == ["claude", "-p", "--model", memory.HAIKU_MODEL]
        assert kwargs["capture_output"] and kwargs["text"]
        calls.append(argv[-1])
        assert responses, "unexpected model call (checks must be deterministic)"
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(returncode=0, stdout=result, stderr="")

    monkeypatch.setattr(memory.subprocess, "run", model_run)

    def write(raw, outputs, **overrides):
        responses.extend(outputs)
        args = dict(text=raw, file="inbox.md", section="Synthetic",
                    source="synthetic-test", dry_run=False, force=False)
        args.update(overrides)
        rc = memory.cmd_write(argparse.Namespace(**args))
        assert not responses, "expected compression/retry was skipped"
        content = target.read_text(encoding="utf-8")
        notes = []
        if db.exists():
            with sqlite3.connect(str(db)) as conn:
                notes = [row[0] for row in conn.execute("SELECT text FROM notes")]
                # Inspect the actual search index, not just the stored file.
                assert conn.execute("SELECT COUNT(*) FROM notes_fts").fetchone()[0] == len(notes)
        return rc, content, notes

    return write, calls


PET_SOURCE = "My pet Brindle is a Scottish Straight."
KO_PET_SOURCE = "\uc6b0\ub9ac \ubc18\ub824\ub3d9\ubb3c \ub2e4\ub78c\uc740 \uc2a4\ucf54\ud2f0\uc2dc \uc2a4\ud2b8\ub808\uc774\ud2b8\uc57c."


def test_explicit_correction_retires_old_fact_before_recall(writer, monkeypatch, tmp_path):
    write, _ = writer
    monkeypatch.setattr(memory, "_supersession_entity_terms", lambda texts: [])
    old = "Momo is a dog"
    new = "Correction: Momo is a cat"
    assert write(old, [old])[0] == 0
    rc, content, notes = write(new, ["Momo is a cat"])
    assert rc == 0 and "[SUPERSEDED] " + old in content
    assert new in content and any("[SUPERSEDED] " + old in note for note in notes)
    monkeypatch.setattr(memory, "TRANSCRIPT_GLOB", str(tmp_path / "none" / "*.jsonl"))
    monkeypatch.setattr(memory, "_ledger_log", lambda line: None)
    recalled = memory.search_core("Momo", k=5, log=False)
    assert recalled and all("dog" not in hit["text"] for hit in recalled)
    assert any("cat" in hit["text"] for hit in recalled)
    lines, withheld = memory._recall_curate([{"text": note} for note in notes])
    assert withheld == 1 and all("dog" not in line for line in lines)
    assert any("cat" in line for line in lines)


@pytest.mark.parametrize("raw,bad", [
    (PET_SOURCE, "My dog Brindle is a Scottish Straight."),
    (PET_SOURCE, "My cat Brindle is a Scottish Straight."),
    (KO_PET_SOURCE, "\ubc18\ub824\uacac \ub2e4\ub78c (\uc2a4\ucf54\ud2f0\uc2dc \uc2a4\ud2b8\ub808\uc774\ud2b8)"),
    (KO_PET_SOURCE, "\ubc18\ub824\ubb18 \ub2e4\ub78c"),
    ("My pet Brindle likes naps.", "My pet Brindle is 7 years old."),
    ("My pet Brindle is 4 years old.", "My pet Brindle is 7 years old."),
    ("I weigh 62.5 kg.", "User weighs 62.6 kg."),
    ("My friend Rowan likes jazz.", "My wife Rowan likes jazz."),
    ("\uce5c\uad6c \uc720\ub9ac\ub294 \uc7ac\uc988\ub97c \uc88b\uc544\ud574\uc694.", "\uc544\ub0b4 \uc720\ub9ac\ub294 \uc7ac\uc988\ub97c \uc88b\uc544\ud568."),
    ("My pet Brindle likes naps.", "My female pet Brindle likes naps."),
    ("I live in Oslo.", "User lives in Bergen."),
    ("My friend Rowan likes jazz.", "My friend Avery likes jazz."),
    ("My pet Nori1 has 2 toys.", "My pet Nori2 has 2 toys."),
    ("I have a dogwood tree.", "User has a dog."),
    ("I enjoy tea.", "User enjoys tea and has a rabbit."),
])
def test_unsupported_attributes_never_reach_markdown_or_index(writer, capsys, raw, bad):
    write, calls = writer
    rc, content, notes = write(" \n" + raw + "\n ", [bad, bad])
    assert rc == 0
    assert raw in content
    assert len(notes) == 1 and notes[0].startswith(raw + " (")
    assert bad not in content and bad not in notes[0]
    assert len(calls) == 2
    assert calls[0] == memory.COMPRESS_PROMPT + raw
    assert calls[1].startswith(memory._GROUND_RETRY_PROMPT)
    assert calls[1].endswith(raw)
    err = capsys.readouterr().err
    assert err.count("compression rejected") == 2
    assert "unsupported" in err and "falling back to raw append" in err
    assert raw not in err and bad not in err


@pytest.mark.parametrize("raw,good", [
    (PET_SOURCE, "Pet Brindle is a Scottish Straight."),
    ("I enjoy cycling.", "The user likes cycling."),
    ("I work in Oslo and enjoy jazz.", "User works in Oslo.\nUser likes jazz."),
    ("My dog Brindle is 4 years old.", "User's dog Brindle is 4-year-old."),
    ("I live in Oslo.", "__USER_LABEL__ lives in Oslo."),
    ("\uc6b0\ub9ac \uac15\uc544\uc9c0 \ub2e4\ub78c\uc740 4\uc0b4\uc774\uc57c.", "\ubc18\ub824\uacac \ub2e4\ub78c 4\uc0b4."),
    ("\uce5c\uad6c \uc720\ub9ac\ub294 \uc7ac\uc988\ub97c \uc88b\uc544\ud574\uc694.", "\uce5c\uad6c \uc720\ub9ac \uc7ac\uc988 \uc88b\uc544\ud568."),
    ("My friend Rowan lives in Oslo.", "- Friend Rowan lives in Oslo."),
    ("My pet Nori1 has 2 toys.", "Pet Nori1 has 2 toys."),
])
def test_faithful_compression_passes_without_retry(writer, capsys, raw, good):
    write, calls = writer
    rc, content, notes = write(raw, [good])
    assert rc == 0
    assert len(calls) == 1
    assert "compression rejected" not in capsys.readouterr().err
    for line in good.splitlines():
        assert line.removeprefix("- ") in content
        assert any(line.removeprefix("- ") in note for note in notes)


def test_stricter_retry_is_checked_and_stored(writer, capsys):
    write, calls = writer
    good = "Pet Brindle is a Scottish Straight."
    bad = "Dog Brindle is a Scottish Straight."
    rc, content, notes = write(PET_SOURCE, [bad, good])
    assert rc == 0 and len(calls) == 2
    assert good in content and any(good in note for note in notes)
    assert bad not in content
    assert capsys.readouterr().err.count("compression rejected") == 1


@pytest.mark.parametrize("retry", ["NONE", "", OSError("synthetic transport failure")])
def test_empty_or_failed_retry_preserves_source(writer, capsys, retry):
    write, calls = writer
    rc, content, notes = write(PET_SOURCE, ["My dog Brindle.", retry])
    assert rc == 0 and len(calls) == 2
    assert PET_SOURCE in content and notes[0].startswith(PET_SOURCE)
    assert "falling back to raw append" in capsys.readouterr().err


def test_force_does_not_bypass_grounding(writer):
    write, calls = writer
    rc, content, notes = write(PET_SOURCE, ["Dog Brindle.", "Dog Brindle."], force=True)
    assert rc == 0 and len(calls) == 2
    assert PET_SOURCE in content and notes[0].startswith(PET_SOURCE)
    assert "Dog Brindle" not in content


def test_one_bad_item_rejects_entire_batch(writer):
    write, calls = writer
    rc, content, notes = write(PET_SOURCE, ["Pet Brindle.\nDog Brindle.", "Dog Brindle."])
    assert rc == 0 and len(calls) == 2
    assert len(notes) == 1 and notes[0].startswith(PET_SOURCE)
    assert "Dog Brindle" not in content


def test_initial_none_still_stores_nothing(writer):
    write, calls = writer
    rc, content, notes = write("The weather is pleasant today.", ["NONE"])
    assert rc == 0 and len(calls) == 1
    assert notes == [] and "weather" not in content


def test_initial_failure_still_falls_back_without_retry(writer):
    write, calls = writer
    rc, content, notes = write(PET_SOURCE, [OSError("synthetic transport failure")])
    assert rc == 0 and len(calls) == 1
    assert PET_SOURCE in content and notes[0].startswith(PET_SOURCE)


def test_dry_run_checks_output_without_writing(writer):
    write, calls = writer
    rc, content, notes = write(PET_SOURCE, ["My dog Brindle.", "My dog Brindle."], dry_run=True)
    assert rc == 0 and len(calls) == 2
    assert PET_SOURCE not in content and notes == []


def test_raw_fallback_still_redacts_secrets(writer):
    write, calls = writer
    raw = "My pet Brindle likes naps; password=synthetic-private."
    rc, content, notes = write(raw, ["My dog Brindle.", "My dog Brindle."])
    assert rc == 0 and len(calls) == 2
    assert "synthetic-private" not in content and "synthetic-private" not in notes[0]
    assert "password=[REDACTED]" in content


def test_existing_fragment_cap_still_refuses_explosion(writer):
    write, calls = writer
    rc, content, notes = write("I enjoy cycling.", ["User likes cycling.\n" * (memory.WRITE_FRAGMENT_CAP + 1)])
    assert rc == 2 and len(calls) == 1
    assert notes == [] and "cycling" not in content


@pytest.mark.parametrize("raw", [
    "# Header\n## Second header\n### Third header",
    "TODO backlog: should implement a durable feature.",
])
def test_existing_input_gates_still_refuse_before_model(writer, raw):
    write, calls = writer
    rc, content, notes = write(raw, [])
    assert rc == 2 and calls == [] and notes == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-rfE"]))
