# test-component: bridge.options
# test-tier: 0
# test-prereq: tmp-files
# test-new-component: Shared prefix-normalization boundary across marker parsing, width checks and send output.
"""Complete menu enumerators are consumed once, before width checks and rendering."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.config import config
from bridge.i18n import t
from bridge.options import (
    EXIT_HATCH_LABELS,
    build_option_keyboard,
    extract_marker_labels,
    extract_options,
    keyboard_labels,
    resolve_choice,
    strip_consumed_options,
    strip_options_marker,
)


DATA = json.loads((Path(__file__).parent / "fixtures/options-numbering.json").read_text())
SHAPES = ("labeled", "trailing", "body")
FAMILIES = tuple(DATA["enumerators"])
FORMATS = ("{}. {}", "{}) {}", "{}\u3001{}", "{}\uff09 {}", "({}) {}", "{} . {}")


def _enumerated(family, fmt="{}. {}", labels=None):
    labels = DATA["menu"] if labels is None else labels
    return [fmt.format(token, label) for token, label in zip(DATA["enumerators"][family], labels)]


def _source(labels, shape):
    if shape == "labeled":
        return "Choose:\n[[OPTIONS: " + " | ".join(labels) + "]]"
    if shape == "trailing":
        return "Choose:\n[[OPTIONS]]\n" + "\n".join(labels)
    return "Choose:\n" + "\n".join(
        f"{i}. {label}" for i, label in enumerate(labels, 1)
    ) + "\n[[OPTIONS]]"


def _parse(text):
    # Match the send path: extract labels from the original before stripping
    # markers, then hand the resulting options to the keyboard builder.
    return strip_consumed_options(
        strip_options_marker(text)[0], marker_labels=extract_marker_labels(text)
    )


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("punct", (".", ")", "\u3001", "\uff09", " ."))
def test_matching_list_number_removed_before_rendering(shape, punct):
    labels = DATA["menu"]
    text = _source([f"{i}{punct} {s}" for i, s in enumerate(labels, 1)], shape)
    display, options = _parse(text)
    assert options == labels
    assert keyboard_labels(text) == labels
    if shape == "body":
        assert extract_options(text) == labels
    assert display == "Choose:"
    keyboard = build_option_keyboard(options)
    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert [b.text for b in buttons] == [f"{i}. {s}" for i, s in enumerate(labels, 1)]
    assert [resolve_choice(b.callback_data, keyboard.inline_keyboard) for b in buttons] == [
        b.text for b in buttons
    ]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("fmt", FORMATS)
def test_dgn2171_ordered_enumerator_family_renders_once(shape, family, fmt):
    labels = DATA["menu"]
    text = _source(_enumerated(family, fmt), shape)
    display, options = _parse(text)
    assert options == labels
    assert keyboard_labels(text) == labels
    assert display == "Choose:"
    assert [row[0].text for row in build_option_keyboard(options).inline_keyboard] == [
        f"{i}. {label}" for i, label in enumerate(labels, 1)
    ]
    if shape == "body":
        assert extract_options(text) == labels


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("family", FAMILIES)
def test_entire_supported_sequence_and_bare_circled_numbers(shape, family):
    labels = [f"choice {i}" for i in range(len(DATA["enumerators"][family]))]
    fmt = "{} {}" if family == "circled" else "({}) {}"
    display, options = _parse(_source(_enumerated(family, fmt, labels), shape))
    assert display == "Choose:"
    assert options == labels


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("broken_at", (0, 1, 3))
@pytest.mark.parametrize("break_kind", ("missing", "wrong_position", "mixed_family", "empty"))
def test_one_broken_label_keeps_every_prefix(shape, family, broken_at, break_kind):
    labels = _enumerated(family)
    if break_kind == "missing":
        labels[broken_at] = DATA["menu"][broken_at]
    elif break_kind == "wrong_position":
        token = DATA["enumerators"][family][(broken_at + 1) % 4]
        labels[broken_at] = token + ". " + DATA["menu"][broken_at]
    elif break_kind == "mixed_family":
        other = "lower" if family != "lower" else "upper"
        labels[broken_at] = _enumerated(other)[broken_at]
    else:
        labels[broken_at] = DATA["enumerators"][family][broken_at] + "."
    _, options = _parse(_source(labels, shape))
    assert options == labels
    assert keyboard_labels(_source(labels, shape)) == labels


@pytest.mark.parametrize("shape", SHAPES)
def test_content_letters_and_unpunctuated_tokens_are_kept(shape):
    for labels in (DATA["content_labels"], ["a recipe", "b salad"], ["A recipe", "B salad"],
                   ["i recipe", "ii salad"], ["1 recipe", "2 salad"], ["(a)", "(b)"]):
        _, options = _parse(_source(labels, shape))
        assert options == labels


@pytest.mark.parametrize("shape", SHAPES)
def test_existing_zero_padded_numeric_sequence_is_preserved(shape):
    display, options = _parse(_source(["01. recipe", "002) salad"], shape))
    assert options == ["recipe", "salad"]
    assert display == "Choose:"


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("locale", ("ko", "en"))
def test_broken_or_mixed_localized_handles_keep_the_entire_menu(shape, locale, monkeypatch):
    monkeypatch.setattr(config, "locale", locale)
    first = t("option_number_handle").format(n=1) + " recipe"
    for second in ("salad", "2. salad", t("option_number_handle").format(n=3) + " salad"):
        labels = [first, second]
        _, options = _parse(_source(labels, shape))
        assert options == labels


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("family", FAMILIES)
def test_only_one_enumerator_layer_is_removed(shape, family):
    inner = _enumerated(family)
    outer = _enumerated(family, labels=inner)
    _, options = _parse(_source(outer, shape))
    assert options == inner


@pytest.mark.parametrize("family", FAMILIES)
def test_mixed_body_block_is_not_deleted_as_a_normalized_duplicate(family):
    labels = _enumerated(family)
    labels[1] = DATA["menu"][1]
    body = "\n".join(labels)
    text = body + "\n\n" + _source(DATA["menu"], "labeled")
    display, options = _parse(text)
    assert options == DATA["menu"]
    assert body in display


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("locale", ("ko", "en"))
def test_localized_handle_prefix_uses_catalog(shape, locale, monkeypatch):
    monkeypatch.setattr(config, "locale", locale)
    labels = DATA["menu"]
    text = _source([
        t("option_number_handle").format(n=i) + " " + s
        for i, s in enumerate(labels, 1)
    ], shape)
    display, options = _parse(text)
    assert options == labels
    assert display == "Choose:"


@pytest.mark.parametrize("shape", SHAPES)
def test_numbers_belonging_to_label_are_kept(shape):
    labels = DATA["numeric_labels"] + [DATA["handle_word"], "5", "No.6"]
    display, options = _parse(_source(labels, shape))
    assert options == labels
    assert display == "Choose:"
    assert [row[0].text for row in build_option_keyboard(options).inline_keyboard] == [
        f"{i}. {s}" for i, s in enumerate(labels, 1)
    ]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("locale", ("ko", "en"))
def test_mismatching_numbers_are_kept(shape, locale, monkeypatch):
    monkeypatch.setattr(config, "locale", locale)
    labels = ["2. recipe", "1) recipe", t("option_number_handle").format(n=4) + " recipe"]
    _, options = _parse(_source(labels, shape))
    assert options == labels


@pytest.mark.parametrize("shape", SHAPES)
def test_only_one_label_prefix_is_removed(shape):
    _, options = _parse(_source(["1. 1. recipe", "2. 2. salad"], shape))
    assert options == ["1. recipe", "2. salad"]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("family", FAMILIES)
def test_normalization_runs_once_before_filtering(shape, family):
    # A removed exit hatch must not shift the positions used by the parser.
    text = _source(_enumerated(family, labels=[sorted(EXIT_HATCH_LABELS)[0], "recipe", "salad"]), shape)
    _, options = _parse(text)
    assert options == ["recipe", "salad"]
    assert build_option_keyboard(options).inline_keyboard[0][0].text == "1. recipe"


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("family", FAMILIES)
def test_width_check_uses_normalized_label_and_preserves_overflow_body(shape, family):
    fitting = ["a" * 28, "recipe"]
    text = _source(_enumerated(family, labels=fitting), shape)
    display, options = _parse(text)
    assert display == "Choose:"
    assert build_option_keyboard(options).inline_keyboard[0][0].text == "1. " + fitting[0]

    long = "a" * 40
    text = _source(_enumerated(family, labels=[long, "recipe"]), shape)
    if shape == "labeled":
        text = "1. " + long + "\n2. recipe\n" + text
    display, options = _parse(text)
    assert long in display
    assert [row[0].text for row in build_option_keyboard(options).inline_keyboard] == [
        t("option_number_handle").format(n=i) for i in (1, 2)
    ]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("family", FAMILIES)
def test_actual_send_renders_menu_once(shape, family):
    from bridge.bot import TelegramBot

    bot = TelegramBot.__new__(TelegramBot)
    bot.application = MagicMock()
    bot.application.bot.send_message = AsyncMock()
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = {}
    labels = DATA["menu"]
    fmt = "{} {}" if family == "circled" else "({}) {}"
    text = _source(_enumerated(family, fmt), shape)
    asyncio.run(bot._send_smart(42, text, force_options=True))
    calls = bot.application.bot.send_message.call_args_list
    keyboards = [c.kwargs["reply_markup"] for c in calls if c.kwargs.get("reply_markup")]
    assert len(keyboards) == 1
    assert [row[0].text for row in keyboards[0].inline_keyboard] == [
        f"{i}. {s}" for i, s in enumerate(labels, 1)
    ]
    bodies = [c.kwargs.get("text", c.args[1] if len(c.args) > 1 else "") for c in calls]
    assert all("[[OPTIONS" not in text for text in bodies)
    assert all(label not in body for label in labels for body in bodies)
