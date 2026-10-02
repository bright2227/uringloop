"""Kernel-backed native/oracle stream gates; no permission-denial skip.

Run this file on a Linux runner with io_uring and local socket permissions.
The deterministic unit suite is separate and cannot substitute for these tests.
"""

import asyncio
import hashlib

import pytest

from uringloop.loop import IoUringProactorEventLoop


class Echo(asyncio.Protocol):
    def connection_made(self, transport):
        self.transport = transport
        transport.set_write_buffer_limits(high=4096, low=1024)

    def data_received(self, data):
        self.transport.write(data)

    def eof_received(self):
        self.transport.write_eof()
        self.transport.close()
        return True


async def echo_scenario(network, path, mode):
    loop = asyncio.get_running_loop()
    if network == "tcp":
        server = await loop.create_server(Echo, "127.0.0.1", 0)
        address = server.sockets[0].getsockname()
    else:
        server = await loop.create_unix_server(Echo, path)
    writer = None
    try:
        if network == "tcp":
            reader, writer = await asyncio.open_connection(*address[:2])
        else:
            reader, writer = await asyncio.open_unix_connection(path)
        transport = writer.transport
        if mode == "native":
            from uringloop.native_stream import NativeStreamTransport

            assert isinstance(transport, NativeStreamTransport)
        transport.set_write_buffer_limits(high=4096, low=1024)
        payload = bytes(range(251)) * 4096
        expected = hashlib.sha256(payload).digest()
        for _ in range(3):
            writer.write(payload)
            await writer.drain()
            received = await reader.readexactly(len(payload))
            assert hashlib.sha256(received).digest() == expected
        writer.write_eof()
        assert await reader.read() == b""
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("mode", ["python", "native"])
@pytest.mark.parametrize("network", ["tcp", "unix"])
def test_one_shot_native_and_oracle_preserve_large_payload_and_half_close(network, mode, tmp_path):
    with asyncio.Runner(loop_factory=lambda: IoUringProactorEventLoop(transports=mode)) as runner:
        runner.run(asyncio.wait_for(echo_scenario(network, str(tmp_path / "echo.sock"), mode), timeout=15))
