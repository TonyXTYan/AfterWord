import asyncio

from afterword.daemon.queue import PlaybackQueue, QueueItem


def item(eid, sid, text="t"):
    return QueueItem(event_id=eid, session_id=sid, text=text)


def test_fifo_across_sessions():
    q = PlaybackQueue(8)
    for i, s in enumerate("abc"):
        assert q.put(item(i, s)).accepted
    assert [q.pop_nowait().session_id for _ in range(3)] == ["a", "b", "c"]
    assert q.pop_nowait() is None


def test_newer_item_supersedes_same_session_in_place():
    q = PlaybackQueue(8)
    q.put(item(1, "a", "old a"))
    q.put(item(2, "b"))
    r = q.put(item(3, "a", "new a"))
    assert r.accepted and r.superseded.event_id == 1 and r.position == 0
    assert [i.event_id for i in (q.pop_nowait(), q.pop_nowait())] == [3, 2]


def test_now_playing_is_not_superseded():
    q = PlaybackQueue(8)
    q.put(item(1, "a"))
    q.now_playing = q.pop_nowait()
    r = q.put(item(2, "a"))
    assert r.accepted and r.superseded is None
    assert q.now_playing.event_id == 1 and len(q) == 1


def test_full_queue_refuses_but_still_supersedes():
    q = PlaybackQueue(2)
    q.put(item(1, "a"))
    q.put(item(2, "b"))
    assert not q.put(item(3, "c")).accepted
    assert q.put(item(4, "a")).superseded.event_id == 1


def test_clear_and_remove():
    q = PlaybackQueue(8)
    for i in range(3):
        q.put(item(i, str(i)))
    assert q.remove(1).event_id == 1
    assert [i.event_id for i in q.clear()] == [0, 2]
    assert len(q) == 0


def test_async_get_waits_for_put():
    async def main():
        q = PlaybackQueue(8)
        task = asyncio.create_task(q.get())
        await asyncio.sleep(0.01)
        assert not task.done()
        q.put(item(7, "x"))
        return await asyncio.wait_for(task, 1)

    assert asyncio.run(main()).event_id == 7
