from app.services.nlp import CONTEXT_MAX, extract_lemmas

TEXT = (
    "The children were running through the garden. Running is a wonderful exercise for children.\n"
    "She quickly opened the old door and the door creaked loudly."
)


def by_lemma(text=TEXT, limit=50):
    return {info.lemma: info for info in extract_lemmas(text, limit)}


def test_counts_lemmas_and_skips_stop_words_and_short_words():
    lemmas = by_lemma()
    assert lemmas["child"].freq == 2
    assert lemmas["run"].freq == 2
    assert lemmas["door"].freq == 2
    assert "the" not in lemmas and "and" not in lemmas and "she" not in lemmas


def test_keeps_only_content_parts_of_speech():
    assert {info.pos for info in extract_lemmas(TEXT, 50)} <= {"NOUN", "VERB", "ADJ", "ADV"}


def test_limit_keeps_the_most_frequent():
    top = extract_lemmas(TEXT, 3)
    assert len(top) == 3
    assert all(info.freq == 2 for info in top)


def test_context_is_a_sentence_containing_the_form():
    info = by_lemma()["garden"]
    assert info.form == "garden"
    assert info.context == "The children were running through the garden."


def test_form_is_the_surface_word_not_the_lemma():
    info = by_lemma("The children ran across the muddy field after the long school day.")["run"]
    assert info.form == "ran"
    assert "ran" in info.context


def test_short_sentence_is_skipped_for_a_better_later_one():
    text = "Ships sail. The old wooden ships sail slowly across the wide grey sea every single morning."
    info = by_lemma(text)["sail"]
    assert info.context.startswith("The old wooden ships")


def test_line_breaks_are_collapsed():
    text = "The quiet river flows\nthrough the ancient\n  forest and beyond the hills."
    info = by_lemma(text)["forest"]
    assert "\n" not in info.context and "  " not in info.context
    assert info.context == "The quiet river flows through the ancient forest and beyond the hills."


def test_long_sentence_is_cut_around_the_word():
    filler = " ".join(["and the river kept flowing past the old mill"] * 12)
    text = f"{filler} until a strange lantern appeared {filler}."
    info = by_lemma(text)["lantern"]
    assert len(info.context) <= CONTEXT_MAX + 2  # the two ellipses
    assert "lantern" in info.context
    assert info.context.startswith("…") and info.context.endswith("…")


def test_empty_text():
    assert extract_lemmas("", 10) == []


def test_glued_together_garbage_is_skipped_because_it_would_not_fit_the_column():
    garbage = "wonderful" * 12  # 108 letters, the kind of "word" a badly extracted PDF produces
    lemmas = by_lemma(f"{garbage} {garbage} The children were running through the garden.")
    assert garbage not in lemmas and "run" in lemmas
    assert all(len(info.lemma) <= 64 and len(info.form) <= 64 for info in lemmas.values())
