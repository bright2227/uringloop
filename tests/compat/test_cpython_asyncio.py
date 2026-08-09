import asyncio
import contextvars
import sys
import threading

import pytest

from uringloop.loop import IoUringProactorEventLoop
from uringloop.proactor import IoUringProactor, _CffiIoUringProactor


@pytest.fixture(params=[IoUringProactor, _CffiIoUringProactor], ids=["native", "cffi-oracle"])
def loop_factory(request):
    return lambda: IoUringProactorEventLoop(request.param())


def run(loop_factory, coroutine):
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(coroutine)


def test_call_soon_callbacks_preserve_fifo_order(loop_factory):
    async def scenario():
        loop = asyncio.get_running_loop()
        called = []
        loop.call_soon(called.append, 1)
        loop.call_soon(called.append, 2)
        await asyncio.sleep(0)
        return called

    assert run(loop_factory, scenario()) == [1, 2]


def test_call_soon_callbacks_scheduled_during_tick_run_next_tick(loop_factory):
    async def scenario():
        loop = asyncio.get_running_loop()
        called = []

        def first():
            called.append("first")
            loop.call_soon(called.append, "nested")

        loop.call_soon(first)
        loop.call_soon(called.append, "peer")
        await asyncio.sleep(0)
        first_tick = tuple(called)
        await asyncio.sleep(0)
        return first_tick, called

    assert run(loop_factory, scenario()) == (("first", "peer"), ["first", "peer", "nested"])


def test_call_soon_captures_context_at_scheduling_time(loop_factory):
    marker = contextvars.ContextVar("phase2_marker", default="default")

    async def scenario():
        loop = asyncio.get_running_loop()
        called = []
        marker.set("scheduled")
        loop.call_soon(lambda: called.append(marker.get()))
        marker.set("mutated")
        await asyncio.sleep(0)
        return called

    assert run(loop_factory, scenario()) == ["scheduled"]


def test_cancelled_timer_never_runs(loop_factory):
    async def scenario():
        loop = asyncio.get_running_loop()
        called = []
        timer = loop.call_later(0.001, called.append, "cancelled")
        timer.cancel()
        await asyncio.sleep(0.01)
        return called

    assert run(loop_factory, scenario()) == []


def test_call_at_past_deadline_runs_promptly(loop_factory):
    async def scenario():
        loop = asyncio.get_running_loop()
        completed = loop.create_future()
        handle = loop.call_at(loop.time() - 1, lambda: completed.set_result(loop.time()))
        result = await asyncio.wait_for(completed, timeout=0.5)
        return handle.when(), result, loop.time()

    deadline, ran_at, finished_at = run(loop_factory, scenario())
    assert deadline <= ran_at <= finished_at


def test_handle_cancel_is_idempotent_and_prevents_callback(loop_factory):
    async def scenario():
        loop = asyncio.get_running_loop()
        called = []
        handle = loop.call_soon(called.append, "unexpected")
        handle.cancel()
        handle.cancel()
        await asyncio.sleep(0)
        return handle.cancelled(), called

    assert run(loop_factory, scenario()) == (True, [])


def test_timer_handle_comparison_and_cancellation(loop_factory):
    async def scenario():
        loop = asyncio.get_running_loop()

        def callback():
            pass

        when = loop.time() + 60
        first = loop.call_at(when, callback)
        same = loop.call_at(when, callback)
        later = loop.call_at(when + 1, callback)
        try:
            before_cancel = (first == same, first < later, later > same, first.when() == when)
            same.cancel()
            after_cancel = first == same
            return before_cancel, after_cancel
        finally:
            first.cancel()
            same.cancel()
            later.cancel()

    assert run(loop_factory, scenario()) == ((True, True, True, True), False)


def test_task_cancellation_preserves_message(loop_factory):
    async def scenario():
        task = asyncio.create_task(asyncio.sleep(60))
        await asyncio.sleep(0)
        task.cancel("compatibility-message")
        with pytest.raises(asyncio.CancelledError, match="compatibility-message"):
            await task

    run(loop_factory, scenario())


def test_streams_round_trip_and_eof(loop_factory):
    async def scenario():
        async def echo(reader, writer):
            data = await reader.readexactly(5)
            writer.write(data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        address = server.sockets[0].getsockname()
        try:
            reader, writer = await asyncio.open_connection(*address[:2])
            writer.write(b"hello")
            await writer.drain()
            assert await reader.readexactly(5) == b"hello"
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    run(loop_factory, scenario())


def test_executor_completion_wakes_idle_loop(loop_factory):
    async def scenario():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, threading.get_ident)

    assert isinstance(run(loop_factory, scenario()), int)


def test_call_soon_threadsafe_captures_producer_context(loop_factory):
    marker = contextvars.ContextVar("phase2_thread_marker", default="default")

    async def scenario():
        loop = asyncio.get_running_loop()
        completed = loop.create_future()

        def callback():
            completed.set_result(marker.get())

        def producer():
            marker.set("producer")
            loop.call_soon_threadsafe(callback)

        thread = threading.Thread(target=producer)
        thread.start()
        try:
            return await asyncio.wait_for(completed, timeout=0.5)
        finally:
            thread.join(timeout=0.5)

    assert run(loop_factory, scenario()) == "producer"


def test_callback_exception_reaches_exception_handler(loop_factory):
    async def scenario():
        loop = asyncio.get_running_loop()
        contexts = []
        loop.set_exception_handler(lambda loop, context: contexts.append(context))

        def fail():
            raise RuntimeError("callback failed")

        loop.call_soon(fail)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return contexts

    [context] = run(loop_factory, scenario())
    assert context["message"].startswith(
        "Exception in callback test_callback_exception_reaches_exception_handler.<locals>.scenario.<locals>.fail()"
    )
    assert isinstance(context["exception"], RuntimeError)


def test_subprocess_exit_and_output(loop_factory):
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "print('compatible')",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode, stdout, stderr

    assert run(loop_factory, scenario()) == (0, b"compatible\n", None)
