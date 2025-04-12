import asyncio
from asyncio import proactor_events
from typing import Any, cast

import pytest

from uringloop.loop import IouringProactorEventLoop


@pytest.mark.asyncio
async def test_unix_connection(unix_socket_path: str):
    event_loop = asyncio.get_running_loop()
    assert isinstance(event_loop, IouringProactorEventLoop)
    # Server protocol
    class EchoServerProtocol(asyncio.Protocol):
        def connection_made(self, transport: asyncio.BaseTransport):
            self.transport = cast(asyncio.WriteTransport, transport)

        def data_received(self, data: bytes):
            self.transport.write(data)
            self.transport.close()

    # Client protocol
    class EchoClientProtocol(asyncio.Protocol):
        def __init__(self, message: str, on_con_lost: asyncio.Future[Any]):
            self.message = message
            self.on_con_lost = on_con_lost
            self.received = None

        def connection_made(self, transport: asyncio.BaseTransport):
            cast(asyncio.WriteTransport, transport).write(self.message.encode())

        def data_received(self, data: bytes):
            self.received = data.decode()

        def connection_lost(self, exc: Exception | None):
            self.on_con_lost.set_result(True)

    # Start server
    server = await event_loop.create_unix_server(
        EchoServerProtocol, path=unix_socket_path
    )
    try:
        # Connect client
        on_con_lost = event_loop.create_future()
        client_transport, client_protocol = await event_loop.create_unix_connection( # type: ignore[reportUnknownVariableType]
            lambda: EchoClientProtocol("HELLO WORLD", on_con_lost),
            path=unix_socket_path
        )
        # Wait for completion
        await on_con_lost
        assert isinstance(client_protocol, EchoClientProtocol)
        assert isinstance(client_transport, proactor_events._ProactorSocketTransport)  # type: ignore[reportPrivateUsage]
        assert client_protocol.received == "HELLO WORLD"

        client_transport.close()
    finally:
        server.close()
        await server.wait_closed()