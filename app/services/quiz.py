import json
import random
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Source, SourceWord, Translation, User, UserWordProgress, Word, WordEmbedding

OPTIONS = 4
SIMILAR_POOL = 6  # distractors are drawn at random from up to this many nearest words...
SIMILAR_SPREAD = 0.15  # ...that are at most this much farther (cosine distance) than the nearest one
QUESTION_TTL = 3600


class StaleQuestion(Exception):
    """The answer does not belong to the currently open question."""


def _q_key(user_id: int) -> str:
    return f"question:{user_id}"


def _recent_key(user_id: int, source_id: int) -> str:
    return f"recent:{user_id}:{source_id}"


async def source_progress(session: AsyncSession, user_id: int, source_id: int) -> dict:
    total = await session.scalar(
        select(func.count()).where(UserWordProgress.user_id == user_id, UserWordProgress.source_id == source_id)
    )
    learned = await session.scalar(
        select(func.count()).where(
            UserWordProgress.user_id == user_id,
            UserWordProgress.source_id == source_id,
            UserWordProgress.correct_count >= settings.correct_to_learn,
        )
    )
    return {"learned_words": learned or 0, "total_words": total or 0}


async def sources_progress(session: AsyncSession, user_id: int, source_ids: list[int]) -> dict[int, dict]:
    """source_progress for several sources in one query."""
    learned = func.coalesce(
        func.sum(case((UserWordProgress.correct_count >= settings.correct_to_learn, 1), else_=0)), 0
    )
    rows = await session.execute(
        select(UserWordProgress.source_id, func.count(), learned)
        .where(UserWordProgress.user_id == user_id, UserWordProgress.source_id.in_(source_ids))
        .group_by(UserWordProgress.source_id)
    )
    result = {sid: {"learned_words": 0, "total_words": 0} for sid in source_ids}
    for source_id, total, done in rows:
        result[source_id] = {"learned_words": int(done), "total_words": total}
    return result


async def forget_source(redis: Redis, user_id: int, source_id: int) -> None:
    """Drop the quiz state that refers to a deleted source."""
    await redis.delete(_recent_key(user_id, source_id))
    raw = await redis.get(_q_key(user_id))
    if raw is not None and json.loads(raw)["source_id"] == source_id:
        await redis.delete(_q_key(user_id))


async def _pick_word(session: AsyncSession, redis: Redis, user_id: int, source_id: int) -> int | None:
    recent = [int(x) for x in await redis.lrange(_recent_key(user_id, source_id), 0, -1)]  # newest first
    # Never repeat the last N words; if too few words remain, relax to "not the previous one", then to anything.
    for excluded in (recent, recent[:1], []):
        stmt = (
            select(UserWordProgress.word_id)
            .where(
                UserWordProgress.user_id == user_id,
                UserWordProgress.source_id == source_id,
                UserWordProgress.correct_count < settings.correct_to_learn,
            )
            .order_by(UserWordProgress.correct_count, func.random())  # least-known words first
            .limit(1)
        )
        if excluded:
            stmt = stmt.where(UserWordProgress.word_id.notin_(excluded))
        word_id = await session.scalar(stmt)
        if word_id is not None:
            return word_id
    return None


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _overlaps(a: str, b: str) -> bool:
    """«бегать» and «бегать трусцой»: one option contains the other, so both may be right."""
    words_a, words_b = set(a.split()), set(b.split())
    return words_a <= words_b or words_b <= words_a


async def _similar_translations(session: AsyncSession, word: Word) -> list[tuple[str, float]]:
    """(translation, distance) of the words closest in meaning (same part of speech), nearest first;
    [] without vectors."""
    if session.get_bind().dialect.name != "postgresql":  # vector distance needs pgvector
        return []
    vector = await session.scalar(select(WordEmbedding.embedding).where(WordEmbedding.word_id == word.id))
    if vector is None:  # not embedded yet (Ollama was down): the worker's embed_loop catches up
        return []
    distance = WordEmbedding.embedding.cosine_distance(vector)
    return list(
        await session.execute(
            select(Translation.text, distance)
            .join(WordEmbedding, WordEmbedding.word_id == Translation.word_id)
            .join(Word, Word.id == Translation.word_id)
            .where(
                Translation.lang == settings.target_lang,
                Translation.word_id != word.id,
                Word.pos == word.pos,
                distance >= settings.distractor_min_distance,  # closer ones are mostly synonyms
            )
            .order_by(distance)
            .limit(SIMILAR_POOL * 3)  # room for the ones the overlap check drops
        )
    )


async def _distractors(session: AsyncSession, source_id: int, word: Word, correct: str) -> list[str]:
    """Wrong options that look plausible: the closest in meaning first, then the same part of speech,
    words of the same source first."""
    lang = settings.target_lang
    picked: list[str] = []
    seen = {_normalize(correct)}

    def acceptable(text: str) -> bool:
        key = _normalize(text)
        return not any(key == s or _overlaps(key, s) for s in seen)

    def take(text: str) -> None:
        seen.add(_normalize(text))
        picked.append(text)

    # Shuffle among the words about as close as the nearest one, so the same word does not always get the very
    # same options; then the rest by closeness. A far word never displaces a closer one.
    similar = await _similar_translations(session, word)
    if similar:
        nearest = similar[0][1]
        pool = [text for text, d in similar[:SIMILAR_POOL] if d <= nearest + SIMILAR_SPREAD]
        for text in random.sample(pool, len(pool)) + [text for text, _ in similar]:
            if len(picked) < OPTIONS - 1 and acceptable(text):
                take(text)

    max_id = await session.scalar(select(func.max(Translation.id))) or 0

    def candidates(same_pos: bool, same_source: bool):
        stmt = (
            select(Translation.text)
            .join(Word, Word.id == Translation.word_id)
            .where(Translation.lang == lang, Translation.word_id != word.id)
        )
        if same_pos:
            stmt = stmt.where(Word.pos == word.pos)
        if same_source:
            stmt = stmt.join(SourceWord, SourceWord.word_id == Translation.word_id).where(
                SourceWord.source_id == source_id
            )
            return stmt.order_by(func.random()).limit(30)  # one source is small, sorting it is cheap
        # The whole dictionary can be huge: look at a random sample of rows instead of sorting all of them.
        sample = random.sample(range(1, max_id + 1), min(300, max_id))
        return stmt.where(Translation.id.in_(sample)).limit(30)

    for same_pos, same_source in ((True, True), (True, False), (False, True), (False, False)):
        if len(picked) == OPTIONS - 1:
            break
        for text in await session.scalars(candidates(same_pos, same_source)):
            if acceptable(text) and len(picked) < OPTIONS - 1:
                take(text)
    return picked


async def next_question(session: AsyncSession, redis: Redis, user: User, source_id: int) -> dict | None:
    """Build a quiz question, or return None when every word of the source is learned."""
    word_id = await _pick_word(session, redis, user.id, source_id)
    if word_id is None:
        return None

    word = await session.get(Word, word_id)
    correct = await session.scalar(
        select(Translation.text).where(Translation.word_id == word_id, Translation.lang == settings.target_lang)
    )
    options = [correct, *await _distractors(session, source_id, word, correct)]
    random.shuffle(options)
    correct_index = options.index(correct)

    await redis.set(
        _q_key(user.id),
        json.dumps({"word_id": word_id, "source_id": source_id, "correct_index": correct_index}),
        ex=QUESTION_TTL,
    )
    recent_key = _recent_key(user.id, source_id)
    await redis.lpush(recent_key, word_id)
    await redis.ltrim(recent_key, 0, settings.recent_window - 1)
    await redis.expire(recent_key, 30 * 24 * 3600)

    source_word = await session.get(SourceWord, (source_id, word_id))
    return {
        "word_id": word_id,
        "word": word.lemma,
        "pos": word.pos,
        "context": source_word.context,
        "form": source_word.form,
        "options": options,
    }


async def submit_answer(
    session: AsyncSession, redis: Redis, user: User, word_id: int, option_index: int
) -> dict:
    raw = await redis.getdel(_q_key(user.id))
    if raw is None:
        raise StaleQuestion
    q = json.loads(raw)
    if q["word_id"] != word_id:
        raise StaleQuestion

    correct = option_index == q["correct_index"]
    progress = await session.get(UserWordProgress, (user.id, q["source_id"], word_id))
    if progress is None:  # the source was deleted after the question was asked
        raise StaleQuestion
    progress.last_asked_at = datetime.now(UTC)
    if correct:
        progress.correct_count += 1
    else:
        progress.wrong_count += 1
        if settings.reset_on_wrong:
            progress.correct_count = 0

    stats = await source_progress(session, user.id, q["source_id"])
    # source_progress ran a query, so the pending change above was flushed by autoflush.
    source_learned = stats["learned_words"] == stats["total_words"]
    if source_learned:
        (await session.get(Source, q["source_id"])).status = "learned"
    await session.commit()

    word = await session.get(Word, word_id)
    correct_text = await session.scalar(
        select(Translation.text).where(Translation.word_id == word_id, Translation.lang == settings.target_lang)
    )
    return {
        "correct": correct,
        "word": word.lemma,
        "correct_translation": correct_text,
        "word_correct_count": progress.correct_count,
        "correct_to_learn": settings.correct_to_learn,
        "source_id": q["source_id"],
        "source_learned": source_learned,
        **stats,
    }
