from asyncio import base_events
from collections import deque

from uringloop import _uringcore
from uringloop.loop import IoUringProactorEventLoop, _NativeReadyQueue


class NullSelector:
    def __init__(self):
        self.timeouts = []

    def select(self, timeout):
        self.timeouts.append(timeout)
        return []


def native_timer_loop(now=1000.0):
    loop = IoUringProactorEventLoop.__new__(IoUringProactorEventLoop)
    base_events.BaseEventLoop.__init__(loop)
    core = _uringcore.LoopCore()
    loop._selector = NullSelector()
    loop._timer_core = core
    loop._timer_heap_mode = "native"
    loop._run_loop_mode = "python"
    loop._ready_queue_mode = "native"
    loop._ready = _NativeReadyQueue(core)
    loop.time = lambda: now
    return loop


def native_tick_loop(now=1000.0, *, ready_budget=4096, timer_budget=4096):
    loop = native_timer_loop(now)
    loop._run_loop_mode = "native"
    loop._native_tick_batch = deque()
    loop._native_tick_immediate = False
    loop._native_cqe_budget = 256
    loop._native_timer_budget = timer_budget
    loop._native_ready_budget = ready_budget
    return loop


def close_test_loop(loop):
    ready = loop._ready
    base_events.BaseEventLoop.close(loop)
    ready.close()


def test_native_timer_heap_promotes_near_due_handles_through_real_run_once():
    loop = native_timer_loop()
    called = []
    try:
        handle = loop.call_at(1000.0 + loop._clock_resolution / 2, called.append, "due")
        loop._run_once()

        assert called == ["due"]
        assert handle._scheduled is False
        assert loop._timer_core.timer_count == 0
        assert 0 <= loop._selector.timeouts[0] <= loop._clock_resolution
    finally:
        close_test_loop(loop)


def test_native_timer_heap_prunes_cancelled_root_before_select_timeout():
    loop = native_timer_loop()
    try:
        cancelled = loop.call_at(1001.0, lambda: None)
        live = loop.call_at(1002.0, lambda: None)
        cancelled.cancel()
        loop._run_once()

        assert loop._selector.timeouts == [2.0]
        assert loop._timer_core.timer_count == 1
        assert loop._timer_core.cancelled_timer_count == 0
        assert live._scheduled is True
    finally:
        close_test_loop(loop)


def test_native_timer_heap_compacts_cancellation_storm():
    loop = native_timer_loop()
    try:
        handles = [loop.call_at(1060.0 + index, lambda: None) for index in range(200)]
        for handle in handles:
            handle.cancel()

        loop.call_soon(lambda: None)
        loop._run_once()

        assert loop._timer_core.timer_count == 0
        assert loop._timer_cancelled_count == 0
    finally:
        close_test_loop(loop)


def test_native_timer_callbacks_schedule_for_the_next_tick():
    loop = native_timer_loop()
    called = []

    def first():
        called.append("first")
        loop.call_at(1000.0, called.append, "nested")

    try:
        loop.call_at(1000.0, first)
        loop._run_once()
        assert called == ["first"]
        loop._run_once()
        assert called == ["first", "nested"]
    finally:
        close_test_loop(loop)


def test_native_tick_applies_ready_budget_and_forces_nonblocking_followup():
    loop = native_tick_loop(ready_budget=2)
    called = []
    try:
        for index in range(5):
            loop.call_soon(called.append, index)

        loop._run_once()
        assert called == [0, 1]
        assert loop._native_tick_immediate is True
        assert loop._selector.timeouts == [0.0]

        loop._run_once()
        loop._run_once()
        assert called == [0, 1, 2, 3, 4]
        assert loop._timer_core.tick_count == 3
    finally:
        close_test_loop(loop)


def test_native_tick_preserves_current_snapshot_and_defers_nested_callback():
    loop = native_tick_loop()
    called = []

    def first():
        called.append("first")
        loop.call_soon(called.append, "nested")

    try:
        loop.call_soon(first)
        loop.call_soon(called.append, "peer")
        loop._run_once()
        assert called == ["first", "peer"]
        loop._run_once()
        assert called == ["first", "peer", "nested"]
    finally:
        close_test_loop(loop)


def test_native_tick_promotes_only_timer_budget_per_iteration():
    loop = native_tick_loop(timer_budget=2)
    called = []
    try:
        for index in range(5):
            loop.call_at(1000.0, called.append, index)

        loop._run_once()
        assert called == [0, 1]
        assert loop._native_tick_immediate is True
        loop._run_once()
        loop._run_once()
        assert called == [0, 1, 2, 3, 4]
        assert loop._timer_core.timer_budget_exhaustions == 2
    finally:
        close_test_loop(loop)
