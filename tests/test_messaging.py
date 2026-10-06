import asyncio

import pytest

from app.messaging import consume, publish

STREAM, GROUP, CONSUMER = "jobs", "workers", "worker-1"


def reader(redis, **kwargs):
    return consume(redis, STREAM, GROUP, CONSUMER, block_ms=20, **kwargs)


async def pending(redis) -> int:
    return (await redis.xpending(STREAM, GROUP))["pending"]


async def test_delivers_messages_in_order(redis):
    await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    for n in range(3):
        await publish(redis, STREAM, {"n": n})
    gen = reader(redis)
    assert [(await gen.__anext__())["n"] for _ in range(3)] == [0, 1, 2]
    await gen.aclose()


async def test_message_is_acknowledged_only_after_the_caller_resumes(redis):
    await publish(redis, STREAM, {"n": 1})
    gen = reader(redis)
    assert await gen.__anext__() == {"n": 1}
    assert await pending(redis) == 1  # still being handled

    with pytest.raises(TimeoutError):  # resuming acks it, then waits for more
        await asyncio.wait_for(gen.__anext__(), 0.3)
    assert await pending(redis) == 0
    await gen.aclose()


async def test_unacknowledged_message_is_replayed_to_the_restarted_consumer(redis):
    await publish(redis, STREAM, {"n": 1})
    await publish(redis, STREAM, {"n": 2})

    crashed = reader(redis)
    assert await crashed.__anext__() == {"n": 1}
    await crashed.aclose()  # died while handling n=1

    restarted = reader(redis)
    assert await restarted.__anext__() == {"n": 1}  # replayed first
    assert await restarted.__anext__() == {"n": 2}
    await restarted.aclose()


async def test_new_messages_arrive_while_waiting(redis):
    gen = reader(redis)
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    await publish(redis, STREAM, {"late": True})
    assert await asyncio.wait_for(task, 1) == {"late": True}
    await gen.aclose()


async def test_stream_is_capped(redis, monkeypatch):
    monkeypatch.setattr("app.messaging.MAX_LEN", 5)
    for n in range(50):
        await publish(redis, STREAM, {"n": n})
    assert await redis.xlen(STREAM) < 50


async def test_malformed_messages_are_dropped_instead_of_blocking_the_stream(redis):
    await redis.xadd(STREAM, {"data": "{not json"})
    await redis.xadd(STREAM, {"other": "field"})
    await publish(redis, STREAM, {"n": 1})
    gen = reader(redis)

    assert await gen.__anext__() == {"n": 1}
    await gen.aclose()
