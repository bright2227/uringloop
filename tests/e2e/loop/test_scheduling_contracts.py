import threading

import pytest

from uringloop.loop import IoUringProactorEventLoop
from uringloop.proactor import IoUringProactor, _CffiIoUringProactor


@pytest.fixture(params=[IoUringProactor, _CffiIoUringProactor], ids=["native", "cffi-oracle"])
def loop(request):
    event_loop = IoUringProactorEventLoop(request.param())
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
