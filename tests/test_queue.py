import asyncio

from afterword.daemon.queue import PlaybackQueue, QueueItem


def item(eid, sid, text="t", turn=None):
    return QueueItem(event_id=eid, session_id=sid, text=text, turn=turn)


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
    assert r.accepted and [i.event_id for i in r.superseded] == [1] and r.position == 0
    assert [i.event_id for i in (q.pop_nowait(), q.pop_nowait())] == [3, 2]


def test_now_playing_is_not_superseded():
    q = PlaybackQueue(8)
    q.put(item(1, "a"))
    q.now_playing = q.pop_nowait()
    r = q.put(item(2, "a"))
    assert r.accepted and r.superseded == []
    assert q.now_playing.event_id == 1 and len(q) == 1


def test_full_queue_refuses_but_still_supersedes():
    q = PlaybackQueue(2)
    q.put(item(1, "a"))
    q.put(item(2, "b"))
    assert not q.put(item(3, "c")).accepted
    assert [i.event_id for i in q.put(item(4, "a")).superseded] == [1]


def test_same_turn_appends_after_that_sessions_items():
    q = PlaybackQueue(8)
    q.put(item(1, "a", turn="t1"))
    q.put(item(2, "b", turn="x"))
    r = q.put(item(3, "a", turn="t1"))
    assert r.accepted and r.superseded == [] and r.position == 1
    q.put(item(4, "a", turn="t1"))
    assert [i.event_id for i in q._items] == [1, 3, 4, 2]


def test_new_turn_supersedes_all_older_turn_items_of_the_session():
    q = PlaybackQueue(8)
    for eid, sid in ((1, "a"), (2, "b"), (3, "a"), (4, "a")):
        q.put(item(eid, sid, turn="t1" if sid == "a" else "x"))
    r = q.put(item(5, "a", turn="t2"))
    assert r.accepted and [i.event_id for i in r.superseded] == [1, 3, 4] and r.position == 0
    assert [i.event_id for i in q._items] == [5, 2]
    q.put(item(6, "a", turn="t2"))  # same new turn: appended
    assert [i.event_id for i in q._items] == [5, 6, 2]


def test_items_without_a_turn_supersede_each_other():
    q = PlaybackQueue(8)
    q.put(item(1, "a"))
    assert [i.event_id for i in q.put(item(2, "a")).superseded] == [1]


def test_full_queue_refuses_a_same_turn_append():
    q = PlaybackQueue(2)
    q.put(item(1, "a", turn="t"))
    q.put(item(2, "b", turn="u"))
    assert not q.put(item(3, "a", turn="t")).accepted
    assert q.put(item(4, "a", turn="t2")).accepted  # a new turn still supersedes


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
