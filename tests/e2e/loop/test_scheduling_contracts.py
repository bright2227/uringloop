import asyncio
import socket
import threading

import pytest

from uringloop.loop import IoUringProactorEventLoop
from uringloop.proactor import IoUringProactor, _CffiIoUringProactor


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


def test_stop_defers_callbacks_scheduled_by_current_batch_until_restart(loop):
    called = []

    def first():
        called.append("first")
        loop.call_soon(called.append, "deferred")
        loop.stop()

    loop.call_soon(first)
    loop.run_forever()
    assert called == ["first"]

    loop.call_soon(loop.stop)
    loop.run_forever()
    assert called == ["first", "deferred"]


def test_debug_loop_rejects_non_threadsafe_scheduling_from_foreign_thread(loop):
    loop.set_debug(True)
    running = threading.Event()
    errors = []

    def producer():
        assert running.wait(timeout=1)
        try:
            loop.call_soon(lambda: None)
        except BaseException as error:
            errors.append(error)
        finally:
            loop.call_soon_threadsafe(loop.stop)

    thread = threading.Thread(target=producer)
    loop.call_soon(running.set)
    loop.call_later(1, loop.stop)
    thread.start()
    try:
        loop.run_forever()
    finally:
        thread.join(timeout=1)

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "Non-thread-safe operation" in str(errors[0])


def test_callback_exception_context_includes_the_failed_handle(loop):
    contexts = []
    loop.set_exception_handler(lambda loop, context: contexts.append(context))

    def fail():
        raise RuntimeError("phase2 contract failure")

    handle = loop.call_soon(fail)
    loop.call_soon(loop.stop)
    loop.run_forever()

    assert len(contexts) == 1
    assert contexts[0]["handle"] is handle
    assert isinstance(contexts[0]["exception"], RuntimeError)
    assert contexts[0]["message"].startswith("Exception in callback")


def test_base_exception_preserves_the_unprocessed_ready_snapshot(loop):
    called = []

    def interrupt():
        raise KeyboardInterrupt

    loop.call_soon(interrupt)
    loop.call_soon(called.append, "preserved")
    with pytest.raises(KeyboardInterrupt):
        loop.run_forever()

    assert called == []
    loop.call_soon(loop.stop)
    loop.run_forever()
    assert called == ["preserved"]


def test_continuously_rescheduled_ready_callback_does_not_starve_io(loop):
    async def scenario():
        receiver, sender = socket.socketpair()
        receiver.setblocking(False)
        sender.setblocking(False)
        spinning = True
        spins = 0

        def spin():
            nonlocal spins
            spins += 1
            if spinning:
                loop.call_soon(spin)

        loop.call_soon(spin)
        try:
            sender.send(b"r")
            data = await asyncio.wait_for(loop.sock_recv(receiver, 1), timeout=0.5)
        finally:
            spinning = False
            receiver.close()
            sender.close()
        await asyncio.sleep(0)
        return data, spins

    data, spins = loop.run_until_complete(scenario())

    assert data == b"r"
    assert 1 <= spins < 100
