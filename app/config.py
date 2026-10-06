from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://tgt:tgt@localhost:5432/tgt"
    redis_url: str = "redis://localhost:6379/0"
    stream_submitted: str = "source.submitted"
    stream_processed: str = "source.processed"
    # sources still "pending" after this long were most likely lost between the DB commit and the queue
    stale_pending_seconds: int = 300

    bot_token: str = ""
    api_url: str = "http://localhost:8000"
    # Shared secret the bot sends in X-API-Token. Empty disables the check (local development only).
    api_token: str = ""
    # Comma-separated Telegram user ids allowed to use the bot; empty means everyone.
    allowed_tg_ids: str = ""
    upload_dir: str = "./data/uploads"

    # Unacknowledged messages belong to a consumer *name*: keep it stable across restarts and unique per replica.
    worker_consumer: str = "worker-1"
    bot_consumer: str = "bot-1"

    # Limits for user-supplied sources
    max_pdf_mb: int = 20
    max_page_mb: int = 5
    fetch_timeout: float = 20.0
    max_redirects: int = 5
    # Sources of one user that may be pending/processing at the same time
    max_active_sources: int = 3
    sources_page_size: int = 20
    # Only for local development against a server on a private address; never enable in production.
    allow_private_urls: bool = False

    translator: str = "ollama"  # ollama | argos
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    target_lang: str = "ru"
    target_lang_name: str = "Russian"
    translate_batch_size: int = 25

    # Quiz distractors are the words closest in meaning (embeddings of the English lemma, see services/embeddings).
    # The vector size is fixed by the schema (EMBED_DIM): after switching to a model of another size or meaning,
    # empty the word_embeddings table so the worker rebuilds it.
    embed_model: str = "nomic-embed-text"
    embed_batch_size: int = 100
    embed_backfill_seconds: int = 300
    # Closer than this (cosine distance) is most likely a synonym, i.e. a second correct answer
    distractor_min_distance: float = 0.2

    correct_to_learn: int = 4
    reset_on_wrong: bool = False
    recent_window: int = 10
    max_words_per_source: int = 300


    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.allowed_tg_ids.replace(";", ",").split(",") if x.strip()}


settings = Settings()
