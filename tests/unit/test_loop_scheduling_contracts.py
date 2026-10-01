import asyncio

import pytest

from uringloop.loop import IoUringProactorEventLoop
from uringloop.proactor import IoUringProactor, _CffiIoUringProactor


class FakeClock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture(
    params=[
        (IoUringProactor, "python", "python", "python"),
        (IoUringProactor, "native", "python", "python"),
        (IoUringProactor, "native", "native", "python"),
        (IoUringProactor, "native", "native", "native"),
        (_CffiIoUringProactor, "python", "python", "python"),
        (_CffiIoUringProactor, "native", "python", "python"),
        (_CffiIoUringProactor, "native", "native", "python"),
        (_CffiIoUringProactor, "native", "native", "native"),
    ],
    ids=[
        "native-python",
        "native-native-ready",
        "native-native-timers",
        "native-native-tick",
        "cffi-python",
        "cffi-native-ready",
        "cffi-native-timers",
        "cffi-native-tick",
    ],
)
def loop(request):
    proactor_factory, ready_queue, timer_heap, run_loop = request.param
    event_loop = IoUringProactorEventLoop(proactor_factory(), ready_queue=ready_queue, timer_heap=timer_heap, run_loop=run_loop)
    try:
        yield event_loop
    finally:
        event_loop.close()


def run_one_tick(loop: asyncio.AbstractEventLoop) -> None:
    loop.call_soon(loop.stop)
    loop.run_forever()


def test_equal_deadline_timers_are_all_serviced_in_the_due_tick(loop):
    clock = FakeClock()
    loop.time = clock
    called = []
    for value in range(8):
        loop.call_at(clock(), called.append, value)

    run_one_tick(loop)

    # CPython deliberately does not promise FIFO ordering between equal
    # deadlines. The contract is that none is lost or deferred.
    assert sorted(called) == list(range(8))


def test_cancellation_during_current_ready_batch_skips_the_peer(loop):
    called = []
    peer = None

    def cancel_peer():
        called.append("canceller")
        peer.cancel()

    loop.call_soon(cancel_peer)
    peer = loop.call_soon(called.append, "cancelled")
    run_one_tick(loop)

    assert called == ["canceller"]


def test_cancelled_timer_heap_is_compacted_without_advancing_clock(loop):
    clock = FakeClock()
    loop.time = clock
    handles = [loop.call_at(clock() + 60, lambda: None) for _ in range(200)]
    for handle in handles:
        handle.cancel()

    run_one_tick(loop)

    assert not loop._scheduled
    assert loop._timer_cancelled_count == 0
    if loop._timer_heap_mode == "native":
        assert loop._timer_core.timer_count == 0


def test_clock_resolution_admits_near_due_timer(loop):
    clock = FakeClock()
    loop.time = clock
    called = []
    loop.call_at(clock() + loop._clock_resolution / 2, called.append, "near-due")

    run_one_tick(loop)

    assert called == ["near-due"]


def test_long_delay_preserves_absolute_deadline(loop):
    clock = FakeClock()
    loop.time = clock
    deadline = clock() + 365 * 24 * 60 * 60

    handle = loop.call_at(deadline, lambda: None)
    try:
        assert handle.when() == deadline
    finally:
        handle.cancel()


def test_debug_handle_repr_records_callback_and_creation_site(loop):
    loop.set_debug(True)

    def callback():
        pass

    handle = loop.call_soon(callback)
    try:
        rendered = repr(handle)
        assert "callback()" in rendered
        assert "created at" in rendered
        assert handle.get_context() is not None
    finally:
        handle.cancel()
