import threading

from uringloop.loop import IoUringProactorEventLoop


class InMemoryProactor:
    def __init__(self):
        self.loop = None
        self.select_calls = 0
        self.closed = False

    def set_loop(self, loop):
        self.loop = loop

    def poll_add(self, file, mask):
        return self.loop.create_future()

    def recv(self, file, size):
        return self.loop.create_future()

    def select(self, timeout=None):
        self.select_calls += 1
        return []

    def close(self):
        self.closed = True


def phase2_loop():
    proactor = InMemoryProactor()
    return IoUringProactorEventLoop(proactor, loop_core="phase2"), proactor


def test_phase2_core_runs_bounded_ready_and_timer_storms_together():
    loop, proactor = phase2_loop()
    called = []
    operations = 10_000
    complete = loop.create_future()

    def callback(index):
        called.append(index)
        if len(called) == operations:
            complete.set_result(None)

    try:
        for index in range(operations // 2):
            loop.call_soon(callback, index)
        now = loop.time()
        for index in range(operations // 2, operations):
            loop.call_at(now, callback, index)

        loop.run_until_complete(complete)

        assert called == list(range(operations))
        assert loop._timer_core.tick_count >= 3
        assert loop._timer_core.ready_budget_exhaustions >= 1
        assert loop._timer_core.timer_budget_exhaustions >= 1
        assert proactor.select_calls == loop._timer_core.tick_count
    finally:
        loop.close()
    assert proactor.closed is True


def test_phase2_core_preserves_stop_restart_snapshot_order():
    loop, _ = phase2_loop()
    called = []

    def first():
        called.append("first")
        loop.call_soon(called.append, "nested")
        loop.stop()

    try:
        loop.call_soon(first)
        loop.call_soon(called.append, "peer")
        loop.run_forever()
        assert called == ["first", "peer"]

        loop.call_soon(loop.stop)
        loop.run_forever()
        assert called == ["first", "peer", "nested"]
    finally:
        loop.close()


def test_phase2_core_delivers_cross_thread_callback_and_closes_eventfd():
    loop, _ = phase2_loop()
    called = []
    producer = threading.Thread(target=loop.call_soon_threadsafe, args=(called.append, "thread"))
    try:
        producer.start()
        producer.join(timeout=1)
        assert not producer.is_alive()

        loop.call_soon(loop.stop)
        loop.run_forever()

        assert called == ["thread"]
        wakeup = loop._eventfd_wakeup
        assert wakeup.signalled is True
    finally:
        loop.close()
    assert wakeup.closed is True
