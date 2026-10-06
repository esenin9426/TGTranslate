"""Turn raw text into a frequency table of learnable lemmas, each with an example sentence."""

import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import spacy

from app.models import LEMMA_MAX

KEEP_POS = {"NOUN", "VERB", "ADJ", "ADV"}
CHUNK_CHARS = 100_000
GOOD_CONTEXT = range(30, 201)  # a sentence of this length reads well as a quiz hint
CONTEXT_MAX = 200


@dataclass
class LemmaInfo:
    lemma: str
    pos: str
    freq: int
    context: str  # a sentence (or a window of a long one) that uses the word
    form: str  # the word as written in `context`


@lru_cache(maxsize=1)
def _nlp():
    # NER and the dependency parser are not needed; tagger + lemmatizer are.
    # The rule-based sentencizer gives sentence boundaries without the parser.
    nlp = spacy.load("en_core_web_sm", disable=["ner", "parser"])
    nlp.add_pipe("sentencizer")
    return nlp


def _chunks(text: str):
    buf, size = [], 0
    for line in text.splitlines():
        buf.append(line)
        size += len(line) + 1
        if size >= CHUNK_CHARS:
            yield "\n".join(buf)
            buf, size = [], 0
    if buf:
        yield "\n".join(buf)


def _context(token) -> tuple[str, str]:
    """The token's sentence on one line; a long sentence is cut to a window around the token."""
    sent = token.sent
    offset = token.idx - sent.start_char
    # Collapse line breaks and repeated spaces on each side separately so the token stays in place.
    before = re.sub(r"\s+", " ", sent.text[:offset]).lstrip()
    after = re.sub(r"\s+", " ", sent.text[offset + len(token.text) :]).rstrip()
    room = CONTEXT_MAX - len(token.text)
    if len(before) + len(after) > room:
        keep_after = min(len(after), max(room // 2, room - len(before)))
        keep_before = min(len(before), room - keep_after)
        if keep_before < len(before):
            before = before[len(before) - keep_before :]
            before = "…" + (before.split(" ", 1)[1] if " " in before else "")  # drop a cut-off first word
        if keep_after < len(after):
            after = after[:keep_after]
            after = (after.rsplit(" ", 1)[0] if " " in after else "") + "…"  # drop a cut-off last word
    return f"{before}{token.text}{after}", token.text


def extract_lemmas(text: str, limit: int) -> list[LemmaInfo]:
    """Return up to `limit` most frequent lemmas with their part of speech and an example sentence."""
    freq: Counter[str] = Counter()
    pos_count: dict[str, Counter[str]] = {}
    example: dict[str, tuple[str, str]] = {}
    has_good_example: set[str] = set()
    for doc in _nlp().pipe(_chunks(text), batch_size=4):
        for tok in doc:
            if not tok.is_alpha or tok.is_stop or tok.pos_ not in KEEP_POS:
                continue
            lemma = tok.lemma_.lower()
            if not 3 <= len(lemma) <= LEMMA_MAX or len(tok.text) > LEMMA_MAX:
                continue  # too short to be useful; too long is glued-together garbage (and would not fit the column)
            freq[lemma] += 1
            pos_count.setdefault(lemma, Counter())[tok.pos_] += 1
            if lemma not in has_good_example:
                context, form = _context(tok)
                if lemma not in example or len(context) in GOOD_CONTEXT:
                    example[lemma] = (context, form)
                if len(context) in GOOD_CONTEXT:
                    has_good_example.add(lemma)
    return [
        LemmaInfo(lemma, pos_count[lemma].most_common(1)[0][0], n, *example[lemma])
        for lemma, n in freq.most_common(limit)
    ]
