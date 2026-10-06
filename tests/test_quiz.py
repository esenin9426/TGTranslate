import json
import math
import os

import pytest

from app.config import settings
from app.models import EMBED_DIM, Source, Translation, UserWordProgress, Word, WordEmbedding
from app.services import quiz

from .conftest import add_words

NOUNS = [(f"noun{i}", "NOUN", f"сущ{i}") for i in range(6)]
VERBS = [(f"verb{i}", "VERB", f"глаг{i}") for i in range(6)]


async def open_question(redis, user, word_id, source_id, correct_index=1):
    payload = {"word_id": word_id, "source_id": source_id, "correct_index": correct_index}
    await redis.set(f"question:{user.id}", json.dumps(payload))


async def answer_correctly(session, redis, user, question):
    open_question = json.loads(await redis.get(f"question:{user.id}"))
    return await quiz.submit_answer(session, redis, user, question["word_id"], open_question["correct_index"])


async def test_question_has_four_unique_options_with_the_correct_one(session, redis, user_source):
    user, source = user_source
    ids = await add_words(session, source.id, user.id, NOUNS)

    q = await quiz.next_question(session, redis, user, source.id)

    assert len(q["options"]) == 4 == len(set(q["options"]))
    lemma = q["word"]
    assert f"сущ{lemma[-1]}" in q["options"]
    assert q["word_id"] == ids[lemma]


async def test_question_carries_pos_and_context(session, redis, user_source):
    user, source = user_source
    await add_words(session, source.id, user.id, NOUNS)
    q = await quiz.next_question(session, redis, user, source.id)
    assert q["pos"] == "NOUN"
    assert q["context"] == f"I {q['word']} it."
    assert q["form"] == q["word"]


async def test_distractors_prefer_the_same_part_of_speech(session, redis, user_source):
    user, source = user_source
    await add_words(session, source.id, user.id, NOUNS + VERBS)
    for _ in range(20):
        q = await quiz.next_question(session, redis, user, source.id)
        expected = "сущ" if q["pos"] == "NOUN" else "глаг"
        assert all(option.startswith(expected) for option in q["options"]), q
        await redis.delete(f"recent:{user.id}:{source.id}")


async def test_distractors_fall_back_to_other_parts_of_speech(session, redis, user_source):
    user, source = user_source
    await add_words(session, source.id, user.id, [("cat", "NOUN", "кошка")] + VERBS[:5])
    q = await quiz.next_question(session, redis, user, source.id)
    while q["word"] != "cat":  # only one noun exists: its options must borrow verbs
        await redis.delete(f"recent:{user.id}:{source.id}")
        q = await quiz.next_question(session, redis, user, source.id)
    assert len(q["options"]) == 4


async def test_distractors_come_from_the_global_dictionary_when_the_source_is_small(session, redis, user_source):
    user, source = user_source
    other = Source(user_id=user.id, kind="url", url="https://example.com/b", title="B", status="ready")
    session.add(other)
    await session.flush()
    await add_words(session, other.id, user.id, NOUNS)
    ids = await add_words(session, source.id, user.id, [("cat", "NOUN", "кошка")])

    q = await quiz.next_question(session, redis, user, source.id)

    assert q["word_id"] == ids["cat"]
    assert len(q["options"]) == 4
    assert set(q["options"]) - {"кошка"} <= {f"сущ{i}" for i in range(6)}


async def test_duplicate_or_case_variant_translations_are_not_offered_together(session, redis, user_source):
    user, source = user_source
    words = [("cat", "NOUN", "Кошка"), ("kitty", "NOUN", "кошка "), ("feline", "NOUN", "КОШКА")]
    words += [(f"n{i}", "NOUN", f"вар{i}") for i in range(4)]
    await add_words(session, source.id, user.id, words)
    for _ in range(15):
        q = await quiz.next_question(session, redis, user, source.id)
        normalized = [" ".join(o.casefold().split()) for o in q["options"]]
        assert len(set(normalized)) == len(normalized), q["options"]
        await redis.delete(f"recent:{user.id}:{source.id}")


async def test_recent_words_are_not_repeated(session, redis, user_source, monkeypatch):
    monkeypatch.setattr(settings, "recent_window", 3)
    user, source = user_source
    await add_words(session, source.id, user.id, NOUNS)
    asked = [(await quiz.next_question(session, redis, user, source.id))["word"] for _ in range(4)]
    assert len(set(asked[:3])) == 3
    assert asked[3] not in asked[1:3]  # never one of the last 3


async def test_repetition_is_allowed_when_too_few_words_remain(session, redis, user_source):
    user, source = user_source
    await add_words(session, source.id, user.id, [("a1", "NOUN", "а"), ("b1", "NOUN", "б")])
    asked = [(await quiz.next_question(session, redis, user, source.id))["word"] for _ in range(4)]
    assert all(asked[i] != asked[i + 1] for i in range(3))  # relaxed to "not the previous one"

    solo = [("only", "NOUN", "один")]
    other_source = Source(user_id=user.id, kind="url", url="https://e.com/c", title="C", status="ready")
    session.add(other_source)
    await session.flush()
    await add_words(session, other_source.id, user.id, solo)
    assert (await quiz.next_question(session, redis, user, other_source.id))["word"] == "only"
    assert (await quiz.next_question(session, redis, user, other_source.id))["word"] == "only"


async def test_least_known_words_come_first(session, redis, user_source, monkeypatch):
    monkeypatch.setattr(settings, "recent_window", 1)
    user, source = user_source
    ids = await add_words(session, source.id, user.id, NOUNS[:3])
    for lemma, count in (("noun0", 2), ("noun1", 1)):
        (await session.get(UserWordProgress, (user.id, source.id, ids[lemma]))).correct_count = count
    await session.commit()
    q = await quiz.next_question(session, redis, user, source.id)
    assert q["word_id"] == ids["noun2"]


async def test_correct_answer_increments_progress(session, redis, user_source):
    user, source = user_source
    await add_words(session, source.id, user.id, NOUNS)
    q = await quiz.next_question(session, redis, user, source.id)

    result = await answer_correctly(session, redis, user, q)

    assert result["correct"] is True
    assert result["word_correct_count"] == 1
    assert result["source_learned"] is False
    assert result["total_words"] == 6 and result["learned_words"] == 0


async def test_wrong_answer_counts_and_keeps_progress_by_default(session, redis, user_source):
    user, source = user_source
    ids = await add_words(session, source.id, user.id, NOUNS)
    row = await session.get(UserWordProgress, (user.id, source.id, ids["noun0"]))
    row.correct_count = 2
    await session.commit()
    await open_question(redis, user, ids["noun0"], source.id)

    result = await quiz.submit_answer(session, redis, user, ids["noun0"], 0)

    assert result["correct"] is False
    assert result["word_correct_count"] == 2
    assert (await session.get(UserWordProgress, (user.id, source.id, ids["noun0"]))).wrong_count == 1


async def test_reset_on_wrong_zeroes_the_counter(session, redis, user_source, monkeypatch):
    monkeypatch.setattr(settings, "reset_on_wrong", True)
    user, source = user_source
    ids = await add_words(session, source.id, user.id, NOUNS)
    (await session.get(UserWordProgress, (user.id, source.id, ids["noun0"]))).correct_count = 3
    await session.commit()
    await open_question(redis, user, ids["noun0"], source.id)

    result = await quiz.submit_answer(session, redis, user, ids["noun0"], 0)

    assert result["word_correct_count"] == 0


async def test_stale_or_repeated_answers_are_rejected(session, redis, user_source):
    user, source = user_source
    ids = await add_words(session, source.id, user.id, NOUNS)
    with pytest.raises(quiz.StaleQuestion):  # no open question
        await quiz.submit_answer(session, redis, user, ids["noun0"], 0)

    q = await quiz.next_question(session, redis, user, source.id)
    with pytest.raises(quiz.StaleQuestion):  # answer for a different word
        other = ids["noun0"] if q["word_id"] != ids["noun0"] else ids["noun1"]
        await quiz.submit_answer(session, redis, user, other, 0)

    q = await quiz.next_question(session, redis, user, source.id)
    await answer_correctly(session, redis, user, q)
    with pytest.raises(quiz.StaleQuestion):  # the question was already answered
        await quiz.submit_answer(session, redis, user, q["word_id"], 0)


async def test_source_becomes_learned_after_every_word_reaches_the_goal(session, redis, user_source, monkeypatch):
    monkeypatch.setattr(settings, "correct_to_learn", 2)
    user, source = user_source
    await add_words(session, source.id, user.id, NOUNS[:2])

    last = None
    for _ in range(4):
        q = await quiz.next_question(session, redis, user, source.id)
        assert q is not None
        last = await answer_correctly(session, redis, user, q)

    assert last["source_learned"] is True
    assert (await session.get(Source, source.id)).status == "learned"
    assert await quiz.next_question(session, redis, user, source.id) is None


async def test_distractors_come_from_a_large_dictionary_without_sorting_all_of_it(session, redis, user_source):
    user, source = user_source
    other = await add_words(session, source.id, user.id, [("target", "NOUN", "цель")])
    # Words of other sources only: the source itself has nothing to offer, so the global sample is used.
    for n in range(1000):
        word = Word(lemma=f"filler{n}", pos="NOUN")
        session.add(word)
        await session.flush()
        session.add(Translation(word_id=word.id, lang="ru", text=f"заполнитель{n}"))
    await session.commit()

    question = await quiz.next_question(session, redis, user, source.id)

    assert other["target"] == question["word_id"]
    assert len(set(question["options"])) == 4


async def test_an_option_that_contains_another_is_not_offered_with_it(session, redis, user_source):
    """«бегать» and «бегать трусцой» could both be right, so they never appear in one question."""
    user, source = user_source
    words = [("run", "VERB", "бегать"), ("jog", "VERB", "бегать трусцой")]
    words += [(f"v{i}", "VERB", f"глагол{i}") for i in range(4)]
    await add_words(session, source.id, user.id, words)
    for _ in range(15):
        q = await quiz.next_question(session, redis, user, source.id)
        assert not {"бегать", "бегать трусцой"} <= set(q["options"]), q["options"]
        await redis.delete(f"recent:{user.id}:{source.id}")


def at_distance(distance: float, axis: int) -> list[float]:
    """A unit vector at the given cosine distance from the first axis."""
    vector = [0.0] * EMBED_DIM
    vector[0] = 1 - distance
    vector[axis] = math.sqrt(1 - vector[0] ** 2)
    return vector


postgres_only = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="vector distance needs Postgres with pgvector"
)


@postgres_only
async def test_distractors_are_the_words_closest_in_meaning_but_not_synonyms(session, redis, user_source):
    user, source = user_source
    words = {
        "big": ("ADJ", "большой", 0.0),
        "huge": ("ADJ", "огромный", 0.05),  # a synonym: too close
        "small": ("ADJ", "маленький", 0.25),
        "tall": ("ADJ", "высокий", 0.3),
        "wide": ("ADJ", "широкий", 0.35),
        "grow": ("VERB", "расти", 0.25),  # close, but another part of speech
    }
    words |= {f"far{i}": ("ADJ", f"далёкий{i}", 0.9) for i in range(6)}
    ids = await add_words(session, source.id, user.id, [(lemma, pos, ru) for lemma, (pos, ru, _) in words.items()])
    for axis, (lemma, (_, _, distance)) in enumerate(words.items(), start=1):
        session.add(WordEmbedding(word_id=ids[lemma], embedding=at_distance(distance, axis)))
    await session.commit()

    q = None
    while q is None or q["word"] != "big":
        await redis.delete(f"recent:{user.id}:{source.id}")
        q = await quiz.next_question(session, redis, user, source.id)

    assert sorted(q["options"]) == sorted(["большой", "маленький", "высокий", "широкий"])


@postgres_only
async def test_a_word_without_a_vector_gets_the_usual_distractors(session, redis, user_source):
    user, source = user_source
    await add_words(session, source.id, user.id, NOUNS)  # nothing embedded yet

    q = await quiz.next_question(session, redis, user, source.id)

    assert len(set(q["options"])) == 4 and all(o.startswith("сущ") for o in q["options"])
