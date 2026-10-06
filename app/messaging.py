"""A tiny at-least-once message queue on top of Redis Streams (replaces Kafka for this small workload)."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from redis.asyncio import Redis
from redis.exceptions import ResponseError

log = logging.getLogger("messaging")

MAX_LEN = 10_000  # approximate cap so the stream cannot grow forever


async def publish(redis: Redis, stream: str, payload: dict) -> None:
    await redis.xadd(stream, {"data": json.dumps(payload)}, maxlen=MAX_LEN, approximate=True)


async def consume(redis: Redis, stream: str, group: str, consumer: str, block_ms: int = 5000) -> AsyncIterator[dict]:
    """Yield decoded messages; each one is acknowledged only after the caller finishes handling it.

    Messages that were delivered to this consumer but never acknowledged (a crash, or an exception in the
    caller's loop body) are yielded again first, so a restarted worker picks up where it died.
    Needs a Redis client created with decode_responses=True.
    """
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    cursor = "0"  # "0" replays this consumer's pending entries, ">" waits for new ones
    while True:
        reply = await redis.xreadgroup(
            group, consumer, {stream: cursor}, count=1, block=block_ms if cursor == ">" else None
        )
        entries = reply[0][1] if reply else []
        if not entries:
            cursor = ">"
            await asyncio.sleep(0)  # never spin without yielding, even if the server returns early
            continue
        for entry_id, fields in entries:
            payload = None
            if fields is not None:  # None: the entry was trimmed while still pending
                try:
                    payload = json.loads(fields["data"])
                except (KeyError, TypeError, ValueError):
                    # A malformed message can never succeed: drop it instead of replaying it forever.
                    log.error("Dropping malformed message %s from %s: %r", entry_id, stream, fields)
            if payload is not None:
                yield payload
            await redis.xack(stream, group, entry_id)
