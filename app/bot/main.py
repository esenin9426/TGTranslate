import asyncio
import html
import io
import logging
import re

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from redis.asyncio import Redis

from app.config import settings
from app.messaging import consume

bot = Bot(settings.bot_token)
dp = Dispatcher()
api = httpx.AsyncClient(
    base_url=settings.api_url, timeout=60, headers={"X-API-Token": settings.api_token} if settings.api_token else {}
)

log = logging.getLogger("bot")

URL_RE = re.compile(r"https?://\S+")
STATUS_ICON = {"pending": "⏳", "processing": "⏳", "ready": "📖", "learned": "✅", "failed": "❌"}
POS_NAME = {"NOUN": "сущ.", "VERB": "глагол", "ADJ": "прил.", "ADV": "нареч."}
UNAVAILABLE = "Сервис временно недоступен, попробуй позже."
DELIVERY_ATTEMPTS = 3
FAILED = "Source is failed"  # the API's answer for a failed source, see /quiz/next
BTN_SOURCES = "📚 Мои источники"
BTN_HELP = "❓ Помощь"
MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_SOURCES), KeyboardButton(text=BTN_HELP)]], resize_keyboard=True
)


class AllowList(BaseMiddleware):
    """Ignore everybody except settings.allowed_tg_ids (when set)."""

    async def __call__(self, handler, event, data):
        allowed = settings.allowed_ids
        user = data.get("event_from_user")
        if allowed and (user is None or user.id not in allowed):
            if isinstance(event, CallbackQuery):
                await event.answer()
            elif isinstance(event, Message):
                await event.answer("Этот бот приватный.")
            return None
        return await handler(event, data)


dp.message.outer_middleware(AllowList())
dp.callback_query.outer_middleware(AllowList())


@dp.errors()
async def on_error(event: ErrorEvent) -> bool:
    """A failed API call or a Telegram error in any handler: tell the user instead of staying silent."""
    log.error("Unhandled error", exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer(UNAVAILABLE, show_alert=True)
        elif update.message:
            await update.message.answer(UNAVAILABLE)
    except Exception:
        log.exception("Could not report the error to the user")
    return True


def _extract_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    link = match.group(0).rstrip(".,;:!?»\"'")
    while link.endswith(")") and link.count(")") > link.count("("):  # "(see https://x.org/a)" but keep wiki/Foo_(bar)
        link = link[:-1]
    return link


def _detail(resp: httpx.Response) -> str | None:
    try:
        detail = resp.json()["detail"]
    except (ValueError, KeyError, TypeError):
        return None
    return detail if isinstance(detail, str) else None


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _quiz_button(source_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("Начать квиз", f"src:{source_id}")]])


def _failed_buttons(source_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button("🔄 Повторить", f"retry:{source_id}"), _button("🗑 Удалить", f"del:{source_id}")]]
    )


def _source_card(source: dict) -> tuple[str, InlineKeyboardMarkup]:
    """A source with the actions that make sense for its status."""
    sid = source["id"]
    text = (
        f"{STATUS_ICON[source['status']]} <b>{html.escape(source['title'])}</b>\n"
        f"Выучено {source['learned_words']}/{source['total_words']}"
    )
    first_row = []
    if source["status"] in ("ready", "learned"):
        first_row.append(_button("▶️ Квиз", f"src:{sid}"))
    elif source["status"] == "failed":
        first_row.append(_button("🔄 Повторить", f"retry:{sid}"))
    if source["status"] != "processing":
        first_row.append(_button("🗑 Удалить", f"del:{sid}"))
    rows = [first_row] if first_row else []
    rows.append([_button("⬅️ К списку", "list")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _quiz_keyboard(q: dict, source_id: int) -> InlineKeyboardMarkup:
    rows = [[_button(opt, f"a:{q['word_id']}:{i}")] for i, opt in enumerate(q["options"])]
    rows.append([_button("⏭ Пропустить", f"skip:{source_id}"), _button("⏹ Закончить", "stop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _highlight(context: str, form: str | None) -> str:
    """HTML-escape the sentence and put the studied word in bold."""
    match = re.search(rf"\b{re.escape(form)}\b", context) if form else None
    if not match:
        return html.escape(context)
    before, word, after = context[: match.start()], match.group(), context[match.end() :]
    return f"{html.escape(before)}<b>{html.escape(word)}</b>{html.escape(after)}"


def format_question(q: dict, learned: int, total: int) -> str:
    pos = POS_NAME.get(q.get("pos", ""))
    header = f"<b>{html.escape(q['word'])}</b>" + (f" ({pos})" if pos else "")
    parts = [header]
    if q.get("context"):
        parts.append(f"«{_highlight(q['context'], q.get('form'))}»")
    parts.append(f"Выучено {learned}/{total}")
    return "\n\n".join(parts)


def _api_error(resp: httpx.Response) -> str:
    """A user-facing reason for a rejected submission."""
    if resp.status_code == 413:
        return f"Файл слишком большой (лимит {settings.max_pdf_mb} МБ)."
    if resp.status_code in (400, 409, 422, 429):
        detail = _detail(resp)
        return f"Не могу принять: {detail}" if detail else "Не похоже на корректную ссылку."
    return UNAVAILABLE


@dp.message(CommandStart())
@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def start(m: Message):
    await m.answer(
        "Пришли ссылку на статью или PDF-книгу — я вытащу слова и устрою по ним квиз.\n"
        "Источник считается выученным, когда каждое слово угадано "
        f"{settings.correct_to_learn} раза.\n\n"
        f"{BTN_SOURCES} (/sources) — список источников: квиз, повтор, удаление.\n"
        "Во время квиза можно пропустить слово или закончить.",
        reply_markup=MAIN_MENU,
    )


async def _fetch_sources(tg_id: int) -> list[dict] | None:
    resp = await api.get("/sources", params={"tg_id": tg_id})
    return resp.json() if resp.status_code == 200 else None


def _sources_list(items: list[dict]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not items:
        return "Пока пусто. Пришли ссылку или PDF.", None
    rows = [
        [_button(f"{STATUS_ICON[s['status']]} {s['title'][:40]} ({s['learned_words']}/{s['total_words']})",
                 f"card:{s['id']}")]
        for s in items
    ]
    return "Твои источники:", InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("sources"))
@dp.message(F.text == BTN_SOURCES)
async def sources(m: Message):
    items = await _fetch_sources(m.from_user.id)
    if items is None:
        await m.answer(UNAVAILABLE)
        return
    text, markup = _sources_list(items)
    await m.answer(text, reply_markup=markup)


async def _edit(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Replace the message the button belongs to, or send a new one if Telegram no longer lets us edit it."""
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except TelegramBadRequest:  # e.g. "message is not modified"
            pass
    await bot.send_message(cb.from_user.id, text, parse_mode="HTML", reply_markup=markup)


@dp.callback_query(F.data == "list")
async def back_to_list(cb: CallbackQuery):
    items = await _fetch_sources(cb.from_user.id)
    if items is None:
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    await cb.answer()
    await _edit(cb, *_sources_list(items))


@dp.callback_query(F.data.startswith("card:"))
async def source_card(cb: CallbackQuery):
    source_id = int(cb.data.split(":")[1])
    items = await _fetch_sources(cb.from_user.id)
    if items is None:
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    source = next((s for s in items if s["id"] == source_id), None)
    if source is None:
        await cb.answer("Источник не найден, возможно он удалён.", show_alert=True)
        return
    await cb.answer()
    await _edit(cb, *_source_card(source))


@dp.message(F.document)
async def pdf(m: Message):
    doc = m.document
    if not (doc.file_name or "").lower().endswith(".pdf"):
        await m.answer("Я принимаю только PDF.")
        return
    if (doc.file_size or 0) > settings.max_pdf_mb * 1024 * 1024:
        await m.answer(f"Файл слишком большой (лимит {settings.max_pdf_mb} МБ).")
        return
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)  # Telegram limits bot downloads to 20 MB
    buf.seek(0)
    files = {"file": (doc.file_name, buf, "application/pdf")}
    try:
        resp = await api.post("/sources/pdf", data={"tg_id": m.from_user.id}, files=files)
    except httpx.HTTPError:
        await m.answer(UNAVAILABLE)
        return
    if resp.status_code != 200:
        await m.answer(_api_error(resp))
        return
    await m.answer("Книга принята, разбираю. Напишу, когда всё будет готово.")


@dp.message(F.text.func(URL_RE.search))  # F.text.regexp() would only match at the very start of the text
async def url(m: Message):
    link = _extract_url(m.text)
    try:
        resp = await api.post("/sources/url", json={"tg_id": m.from_user.id, "url": link})
    except httpx.HTTPError:
        await m.answer(UNAVAILABLE)
        return
    if resp.status_code != 200:
        await m.answer(_api_error(resp))
        return
    if resp.json().get("duplicate"):
        await m.answer("Эта ссылка уже добавлена, смотри /sources.")
        return
    await m.answer("Ссылка принята, разбираю. Напишу, когда всё будет готово.")


async def send_question(message: Message, tg_id: int, source_id: int) -> None:
    resp = await api.post("/quiz/next", json={"tg_id": tg_id, "source_id": source_id})
    if resp.status_code == 404:
        await message.answer("Источник не найден, возможно он удалён.")
        return
    if resp.status_code == 409:
        if _detail(resp) == FAILED:
            await message.answer("Этот источник не удалось обработать.", reply_markup=_failed_buttons(source_id))
        else:
            await message.answer("Источник ещё обрабатывается, попробуй чуть позже.")
        return
    if resp.status_code != 200:
        await message.answer(UNAVAILABLE)
        return
    data = resp.json()
    if data["done"]:
        await message.answer(
            f"🎉 «{html.escape(data['title'])}» выучен! Все слова угаданы {settings.correct_to_learn} раза.",
            parse_mode="HTML",
        )
        return
    q = data["question"]
    await message.answer(
        format_question(q, data["learned_words"], data["total_words"]),
        parse_mode="HTML",
        reply_markup=_quiz_keyboard(q, source_id),
    )


@dp.callback_query(F.data.startswith("src:"))
async def pick_source(cb: CallbackQuery):
    await cb.answer()
    if isinstance(cb.message, Message):  # otherwise the message is too old for Telegram to show it to us
        await send_question(cb.message, cb.from_user.id, int(cb.data.split(":")[1]))


@dp.callback_query(F.data.startswith("retry:"))
async def retry_source(cb: CallbackQuery):
    await cb.answer()
    resp = await api.post(f"/sources/{int(cb.data.split(':')[1])}/retry", params={"tg_id": cb.from_user.id})
    if resp.status_code == 200:
        await cb.message.answer("Запустил обработку заново. Напишу, когда всё будет готово.")
    else:
        await cb.message.answer(_api_error(resp))


@dp.callback_query(F.data.startswith("del:"))
async def confirm_delete(cb: CallbackQuery):
    await cb.answer()
    source_id = int(cb.data.split(":")[1])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[_button("✅ Да, удалить", f"delok:{source_id}"), _button("Отмена", f"card:{source_id}")]]
    )
    await _edit(cb, "Удалить источник вместе с прогрессом по его словам?", keyboard)


@dp.callback_query(F.data.startswith("delok:"))
async def delete_source(cb: CallbackQuery):
    await cb.answer()
    resp = await api.delete(f"/sources/{int(cb.data.split(':')[1])}", params={"tg_id": cb.from_user.id})
    if resp.status_code != 200:
        await _edit(cb, html.escape(_api_error(resp)))
        return
    await _edit(cb, "Источник удалён.", InlineKeyboardMarkup(inline_keyboard=[[_button("⬅️ К списку", "list")]]))


@dp.callback_query(F.data.startswith("skip:"))
async def skip_word(cb: CallbackQuery):
    """Ask another word; the skipped one is not counted and, being recent, does not come back right away."""
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await send_question(cb.message, cb.from_user.id, int(cb.data.split(":")[1]))


@dp.callback_query(F.data == "stop")
async def stop_quiz(cb: CallbackQuery):
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer("Квиз остановлен, прогресс сохранён.", reply_markup=MAIN_MENU)


@dp.callback_query(F.data.startswith("a:"))
async def answer(cb: CallbackQuery):
    _, word_id, idx = cb.data.split(":")
    resp = await api.post(
        "/quiz/answer", json={"tg_id": cb.from_user.id, "word_id": int(word_id), "option_index": int(idx)}
    )
    if resp.status_code == 409:
        await cb.answer("Этот вопрос уже неактуален", show_alert=True)
        return
    if resp.status_code != 200 or not isinstance(cb.message, Message):
        await cb.answer(UNAVAILABLE, show_alert=True)
        return
    r = resp.json()
    await cb.answer()
    mark = "✅ Верно" if r["correct"] else "❌ Неверно"
    await cb.message.edit_text(
        f"{mark}: <b>{html.escape(r['word'])}</b> — {html.escape(r['correct_translation'])}\n"
        f"Слово: {min(r['word_correct_count'], r['correct_to_learn'])}/{r['correct_to_learn']}",
        parse_mode="HTML",
    )
    await send_question(cb.message, cb.from_user.id, r["source_id"])


async def notify(event: dict) -> None:
    title = html.escape(event["title"])
    if event["status"] == "ready":
        await bot.send_message(
            event["tg_id"],
            f"✅ «{title}» готово: {event['words']} слов для изучения.",
            parse_mode="HTML",
            reply_markup=_quiz_button(event["source_id"]),
        )
    else:
        await bot.send_message(
            event["tg_id"],
            f"❌ Не удалось обработать «{title}»: {html.escape(event['error'])}",
            parse_mode="HTML",
        )


async def deliver(event: dict) -> None:
    """Send a notification. Temporary Telegram trouble is retried; permanent errors (the user blocked the bot)
    are dropped, because retrying cannot help and one bad event must not stop the others."""
    for attempt in range(1, DELIVERY_ATTEMPTS + 1):
        try:
            await notify(event)
            return
        except (TelegramForbiddenError, TelegramBadRequest):
            log.warning("Could not deliver %s: rejected by Telegram", event, exc_info=True)
            return
        except TelegramRetryAfter as exc:
            delay = exc.retry_after
        except (TelegramNetworkError, TelegramServerError):
            delay = 2**attempt
        except Exception:
            log.exception("Could not deliver %s", event)
            return
        await asyncio.sleep(delay)
    log.error("Giving up delivering %s after %d attempts", event, DELIVERY_ATTEMPTS)


async def notify_loop(redis: Redis) -> None:
    while True:
        try:
            async for event in consume(redis, settings.stream_processed, "bot", settings.bot_consumer):
                await deliver(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # e.g. Redis restarted. Without this the task would die silently and notifications would stop for good.
            log.exception("Notification loop failed, retrying in 5 s")
            await asyncio.sleep(5)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # socket_timeout must exceed consume()'s block_ms
    redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=30)
    notifier = asyncio.create_task(notify_loop(redis))
    try:
        await dp.start_polling(bot)
    finally:
        notifier.cancel()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
