import socket

import pytest

from uringloop.proactor import IoUringProactor


@pytest.mark.asyncio
async def test_recv_into_pins_buffer_until_completion(init_proactor: IoUringProactor):
    receiver, sender = socket.socketpair()
    receiver.setblocking(False)
    sender.setblocking(False)
    buffer = bytearray(4)

    try:
        future = init_proactor.recv_into(receiver, buffer)

        with pytest.raises(BufferError):
            buffer.extend(b"x")

        sender.send(b"test")
        assert await future == 4

        buffer.extend(b"x")
        assert buffer == b"testx"
    finally:
        receiver.close()
        sender.close()
