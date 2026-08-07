import asyncio
import sys
import threading

import pytest

from uringloop.loop import IoUringProactorEventLoop


def run(coroutine):
    with asyncio.Runner(loop_factory=IoUringProactorEventLoop) as runner:
        return runner.run(coroutine)


def test_call_soon_callbacks_preserve_fifo_order():
    async def scenario():
        loop = asyncio.get_running_loop()
        called = []
        loop.call_soon(called.append, 1)
        loop.call_soon(called.append, 2)
        await asyncio.sleep(0)
        return called

    assert run(scenario()) == [1, 2]


def test_cancelled_timer_never_runs():
    async def scenario():
        loop = asyncio.get_running_loop()
        called = []
        timer = loop.call_later(0.001, called.append, "cancelled")
        timer.cancel()
        await asyncio.sleep(0.01)
        return called

    assert run(scenario()) == []


def test_task_cancellation_preserves_message():
    async def scenario():
        task = asyncio.create_task(asyncio.sleep(60))
        await asyncio.sleep(0)
        task.cancel("compatibility-message")
        with pytest.raises(asyncio.CancelledError, match="compatibility-message"):
            await task

    run(scenario())


def test_streams_round_trip_and_eof():
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

    run(scenario())


def test_executor_completion_wakes_idle_loop():
    async def scenario():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, threading.get_ident)

    assert isinstance(run(scenario()), int)


def test_callback_exception_reaches_exception_handler():
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

    [context] = run(scenario())
    assert context["message"].startswith(
        "Exception in callback test_callback_exception_reaches_exception_handler.<locals>.scenario.<locals>.fail()"
    )
    assert isinstance(context["exception"], RuntimeError)


def test_subprocess_exit_and_output():
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "print('compatible')",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode, stdout, stderr

    assert run(scenario()) == (0, b"compatible\n", None)
