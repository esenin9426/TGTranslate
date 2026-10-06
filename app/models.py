from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Column sizes. Postgres rejects longer values (SQLite does not), so producers must cut or skip to these limits.
TITLE_MAX = 300
LEMMA_MAX = 64
TRANSLATION_MAX = 200
EMBED_DIM = 768  # nomic-embed-text


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Source(Base):
    """An article (url) or a book (pdf) submitted by a user."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # url | pdf
    title: Mapped[str] = mapped_column(String(TITLE_MAX), default="")
    url: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|processing|ready|learned|failed
    error: Mapped[str | None] = mapped_column(Text)
    # Whether the user was told about the outcome (ready/failed); the worker re-sends it until it is recorded.
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Word(Base):
    """Global dictionary of lemmas shared by all users and sources."""

    __tablename__ = "words"

    id: Mapped[int] = mapped_column(primary_key=True)
    lemma: Mapped[str] = mapped_column(String(LEMMA_MAX), unique=True, index=True)
    pos: Mapped[str] = mapped_column(String(8))


class WordEmbedding(Base):
    """Meaning vector of a lemma (settings.embed_model); quiz distractors are its nearest neighbours.

    No ANN index: the dictionary is small enough for an exact scan, and an HNSW scan would drop rows that the
    part-of-speech and synonym filters reject after it.
    """

    __tablename__ = "word_embeddings"

    word_id: Mapped[int] = mapped_column(ForeignKey("words.id"), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))


class Translation(Base):
    __tablename__ = "translations"
    __table_args__ = (UniqueConstraint("word_id", "lang"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("words.id"), index=True)
    lang: Mapped[str] = mapped_column(String(8))
    text: Mapped[str] = mapped_column(String(TRANSLATION_MAX))


class SourceWord(Base):
    __tablename__ = "source_words"

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("words.id"), primary_key=True)
    freq: Mapped[int] = mapped_column(Integer, default=1)
    context: Mapped[str | None] = mapped_column(Text)  # a sentence from the source that uses the word
    form: Mapped[str | None] = mapped_column(String(LEMMA_MAX))  # the word exactly as written in that sentence


class UserWordProgress(Base):
    """Per user + source + word learning state."""

    __tablename__ = "user_word_progress"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("words.id"), primary_key=True)
    correct_count: Mapped[int] = mapped_column(Integer, default=0)
    wrong_count: Mapped[int] = mapped_column(Integer, default=0)
    last_asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
