import httpx
import pytest

from app.bot.main import URL_RE, _api_error, _extract_url, _highlight, _quiz_keyboard, _source_card, format_question


def test_highlight_bolds_the_studied_word_and_escapes_html():
    assert _highlight("She was <running> fast & far.", "running") == "She was &lt;<b>running</b>&gt; fast &amp; far."


def test_highlight_matches_whole_words_only():
    assert _highlight("The cat concatenated it.", "cat") == "The <b>cat</b> concatenated it."


@pytest.mark.parametrize("form", [None, "", "missing"])
def test_highlight_without_a_usable_form_still_escapes(form):
    assert _highlight("a < b", form) == "a &lt; b"


def test_question_shows_word_part_of_speech_context_and_progress():
    text = format_question(
        {"word": "run", "pos": "VERB", "context": "They ran home.", "form": "ran"}, learned=3, total=120
    )
    assert text == "<b>run</b> (глагол)\n\n«They <b>ran</b> home.»\n\nВыучено 3/120"


def test_question_survives_missing_context_and_unknown_pos():
    text = format_question({"word": "a<b", "pos": "X", "context": None, "form": None}, learned=0, total=1)
    assert text == "<b>a&lt;b</b>\n\nВыучено 0/1"


def response(status, json=None):
    return httpx.Response(status, json=json)


def test_api_errors_are_translated_for_the_user():
    assert "слишком большой" in _api_error(response(413))
    assert "The link points to a non-public address" in _api_error(
        response(400, {"detail": "The link points to a non-public address"})
    )
    assert "корректную ссылку" in _api_error(response(422, {"detail": [{"msg": "invalid url"}]}))
    assert "недоступен" in _api_error(response(503))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://example.com/a", "https://example.com/a"),
        ("смотри https://example.com/a.", "https://example.com/a"),
        ("(see https://example.com/a)", "https://example.com/a"),
        ("https://en.wikipedia.org/wiki/Foo_(bar)).", "https://en.wikipedia.org/wiki/Foo_(bar)"),
        ("no link here", None),
    ],
)
def test_link_is_cut_out_of_the_message(text, expected):
    assert _extract_url(text) == expected


def test_a_link_in_the_middle_of_a_message_is_recognised_by_the_handler_filter():
    from aiogram import F

    class Message:
        text = "смотри https://example.com/a"

    assert F.text.func(URL_RE.search).resolve(Message)


def test_quota_and_conflict_errors_are_explained():
    assert "Too many" in _api_error(response(429, {"detail": "Too many sources"}))
    assert "no longer available" in _api_error(response(409, {"detail": "The uploaded file is no longer available"}))


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ready", ["src:7", "del:7", "list"]),
        ("learned", ["src:7", "del:7", "list"]),
        ("failed", ["retry:7", "del:7", "list"]),
        ("pending", ["del:7", "list"]),
        ("processing", ["list"]),  # the API refuses to delete a source while it is being processed
    ],
)
def test_source_card_offers_actions_that_fit_the_status(status, expected):
    source = {"id": 7, "title": "A <b>", "status": status, "learned_words": 1, "total_words": 5}
    text, markup = _source_card(source)
    assert "A &lt;b&gt;" in text and "1/5" in text
    assert _callbacks(markup) == expected


def test_quiz_keyboard_has_options_then_skip_and_stop():
    markup = _quiz_keyboard({"word_id": 3, "options": ["a", "b"]}, source_id=9)
    assert _callbacks(markup) == ["a:3:0", "a:3:1", "skip:9", "stop"]
