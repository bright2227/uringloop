import asyncio
import io
import os
import socket
import tempfile
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio

from uringloop.proactor import IoUringProactor


@pytest.fixture
def unix_socket_path():
    with tempfile.TemporaryDirectory() as f:
        yield os.path.join(f, "test.sock")


@pytest.fixture
def test_file() -> Generator[io.BufferedReader, None, None]:
    """Fixture to create and clean up a test file."""
    # Create a temporary file with test data
    with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as f:
        # Write some test data
        test_data = b"File content line 1\nLine 2\nBinary data: \x00\xff\xaa\xbb"
        f.write(test_data)
        f.flush()
        # Reopen in read mode for sending
        tf = open(f.name, "rb")
        yield tf
        tf.close()


@pytest.fixture
def client_tcp_sock() -> Generator[socket.socket, None, None]:
    """Fixture to create and close a client socket for each test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    yield sock
    sock.close()


@pytest.fixture
def server_tcp_sock() -> Generator[socket.socket, None, None]:
    """Fixture to create and close a client socket for each test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    address = ("127.0.0.1", 0)
    sock.bind(address)
    sock.listen(5)
    yield sock
    sock.close()



@pytest.fixture
def client_udp_sock() -> Generator[socket.socket, None, None]:
    """Fixture to create and close a client socket for each test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    yield sock
    sock.close()


@pytest.fixture
def server_udp_sock() -> Generator[socket.socket, None, None]:
    """Fixture to create and close a server socket for each test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    address = ("127.0.0.1", 0)  # Let OS choose port
    sock.bind(address)
    yield sock
    sock.close()


@pytest_asyncio.fixture
async def init_proactor() -> AsyncGenerator[IoUringProactor, None]:
    """Fixture to create and clean up the proactor and its polling task."""
    loop = asyncio.get_event_loop()
    proactor = IoUringProactor()
    proactor.set_loop(loop)

    # Create a task that polls the proactor frequently
    # TODO break test when timeout or _run_proactor_task stops
    async def _run_proactor_task():
        while True:
            proactor._poll(timeout=0.0)  # type: ignore[reportPrivateUsage]
            await asyncio.sleep(0.1)

    task = loop.create_task(_run_proactor_task())

    def stop_all_coro_if_raise_exception(task: asyncio.Task[None]):
        if task.exception():  # If task failed
            # Cancel all other tasks
            for t in asyncio.all_tasks(loop):
                if t != task and not t.done():
                    t.cancel()

            loop.run_until_complete(loop.shutdown_asyncgens())

    # Add failure callback
    task.add_done_callback(stop_all_coro_if_raise_exception)

    yield proactor

    # Cleanup
    proactor.close()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
