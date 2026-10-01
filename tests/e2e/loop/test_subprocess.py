import asyncio
import errno
import os
from typing import Any

import pytest

from uringloop.loop import IoUringProactorEventLoop, _IoUringWritePipeTransport  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_subprocess_basic():
    event_loop = asyncio.get_running_loop()
    assert isinstance(event_loop, IoUringProactorEventLoop)

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
    assert isinstance(event_loop, IoUringProactorEventLoop)

    class SubprocessProtocol(asyncio.SubprocessProtocol):
        def __init__(self, exit_future: asyncio.Future[Any]):
            self.exit_future = exit_future
            self.output = b""

        def pipe_data_received(self, fd: int, data: bytes):
            self.output += data

        def process_exited(self):
            self.exit_future.set_result(True)

    exit_future = event_loop.create_future()
    transport, protocol = await event_loop.subprocess_exec(lambda: SubprocessProtocol(exit_future), "/bin/cat")

    # Write to stdin and close
    stdin = transport.get_pipe_transport(0)
    assert isinstance(stdin, _IoUringWritePipeTransport), f"stdin got unexpected type {type(stdin)}"
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
    assert isinstance(event_loop, IoUringProactorEventLoop)

    class SubprocessProtocol(asyncio.SubprocessProtocol):
        def __init__(self, exit_future: asyncio.Future[Any]):
            self.exit_future = exit_future

        def process_exited(self):
            self.exit_future.set_result(True)

    exit_future = event_loop.create_future()
    transport, _ = await event_loop.subprocess_exec(lambda: SubprocessProtocol(exit_future), "/bin/sh", "-c", "exit 42")

    await exit_future
    assert transport.get_returncode() == 42
    transport.close()


@pytest.mark.asyncio
async def test_subprocess_without_os_pidfd_open(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)

    process = await asyncio.create_subprocess_exec("/bin/sh", "-c", "exit 7")

    assert await process.wait() == 7


@pytest.mark.asyncio
async def test_subprocess_when_pidfd_open_fails(monkeypatch: pytest.MonkeyPatch):
    def unavailable_pidfd(pid: int):
        raise OSError(errno.EPERM, "pidfd_open blocked", pid)

    monkeypatch.setattr(os, "pidfd_open", unavailable_pidfd, raising=False)

    process = await asyncio.create_subprocess_exec("/bin/sh", "-c", "exit 8")

    assert await process.wait() == 8


@pytest.mark.asyncio
async def test_subprocess_when_pidfd_poll_registration_fails(monkeypatch: pytest.MonkeyPatch):
    event_loop = asyncio.get_running_loop()
    assert isinstance(event_loop, IoUringProactorEventLoop)
    if not hasattr(os, "pidfd_open"):
        pytest.skip("os.pidfd_open is unavailable")

    opened_pidfds = set()
    pidfd_open = os.pidfd_open
    poll_add = event_loop._proactor.poll_add  # type: ignore[reportPrivateUsage]

    def tracked_pidfd_open(pid: int):
        fd = pidfd_open(pid)
        opened_pidfds.add(fd)
        return fd

    def failing_poll_add(file, events):
        if isinstance(file, int) and file in opened_pidfds:
            raise OSError(errno.EIO, "pidfd poll registration failed")
        return poll_add(file, events)

    monkeypatch.setattr(os, "pidfd_open", tracked_pidfd_open)
    monkeypatch.setattr(event_loop._proactor, "poll_add", failing_poll_add)  # type: ignore[reportPrivateUsage]

    process = await asyncio.create_subprocess_exec("/bin/sh", "-c", "exit 9")

    assert await process.wait() == 9
    assert len(opened_pidfds) == 1
    with pytest.raises(OSError) as error:
        os.fstat(opened_pidfds.pop())
    assert error.value.errno == errno.EBADF
