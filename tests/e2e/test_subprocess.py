import asyncio
from typing import Any

import pytest

from uringloop.loop import IouringProactorEventLoop, _IouringWritePipeTransport  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_subprocess_basic():
    event_loop = asyncio.get_running_loop()
    assert isinstance(event_loop, IouringProactorEventLoop)
    # Create a protocol class that tracks completion
    class SubprocessProtocol(asyncio.SubprocessProtocol):
        def __init__(self, exit_future: asyncio.Future[Any]):
            self.exit_future = exit_future
            self.output = b""

        def pipe_data_received(self, fd: int, data: bytes):
            self.output += data

        def process_exited(self):
            self.exit_future.set_result(True)

    exit_future = event_loop.create_future()
    transport, protocol = await event_loop.subprocess_exec(
        lambda: SubprocessProtocol(exit_future), "/bin/sh", "-c", "sleep 3 && echo hello"
    )

    # Wait for process to exit
    await exit_future
    assert transport.get_returncode() == 0
    assert protocol.output.strip() == b"hello"
    transport.close()


@pytest.mark.asyncio
async def test_subprocess_io():
    event_loop = asyncio.get_running_loop()
    assert isinstance(event_loop, IouringProactorEventLoop)
    class SubprocessProtocol(asyncio.SubprocessProtocol):
        def __init__(self, exit_future: asyncio.Future[Any]):
            self.exit_future = exit_future
            self.output = b""

        def pipe_data_received(self, fd: int, data: bytes):
            self.output += data

        def process_exited(self):
            self.exit_future.set_result(True)

    exit_future = event_loop.create_future()
    transport, protocol = await event_loop.subprocess_exec(
        lambda: SubprocessProtocol(exit_future), "/bin/cat"
    )

    # Write to stdin and close
    stdin = transport.get_pipe_transport(0)
    assert isinstance(stdin, _IouringWritePipeTransport), f"stdin got unexpected type {type(stdin)}"
    stdin.write(b"test data\n")
    stdin.close()

    # Wait for process to exit
    await exit_future
    assert transport.get_returncode() == 0
    assert protocol.output.strip() == b"test data"
    transport.close()


@pytest.mark.asyncio
async def test_subprocess_error():
    event_loop = asyncio.get_running_loop()
    assert isinstance(event_loop, IouringProactorEventLoop)
    class SubprocessProtocol(asyncio.SubprocessProtocol):
        def __init__(self, exit_future: asyncio.Future[Any]):
            self.exit_future = exit_future

        def process_exited(self):
            self.exit_future.set_result(True)

    exit_future = event_loop.create_future()
    transport, _ = await event_loop.subprocess_exec(
        lambda: SubprocessProtocol(exit_future), "/bin/sh", "-c", "exit 42"
    )

    await exit_future
    assert transport.get_returncode() == 42
    transport.close()
