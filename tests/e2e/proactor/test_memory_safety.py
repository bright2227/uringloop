import asyncio
import errno
import socket
from types import SimpleNamespace

import pytest

from uringloop.native_proactor import NativeIoUringProactor
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


@pytest.mark.asyncio
async def test_cancel_before_first_poll_drains_native_request(init_proactor: IoUringProactor):
    receiver, sender = socket.socketpair()
    receiver.setblocking(False)
    sender.setblocking(False)
    try:
        future = init_proactor.recv(receiver, 1)

        assert future.cancel() is True
        init_proactor._poll(0.1)  # type: ignore[reportPrivateUsage]

        assert future.cancelled() is True
        assert init_proactor._cache == {}  # type: ignore[reportPrivateUsage]
    finally:
        receiver.close()
        sender.close()


@pytest.mark.asyncio
async def test_native_cancel_failure_is_reported(init_proactor: IoUringProactor):
    if not isinstance(init_proactor, NativeIoUringProactor):
        pytest.skip("native cancellation completion handling")
    receiver, sender = socket.socketpair()
    receiver.setblocking(False)
    contexts = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: contexts.append(context))
    try:
        future = init_proactor.recv(receiver, 1)
        assert future.cancel() is True
        cancellation = next(request for request in init_proactor._cache if request.kind == "cancel")  # type: ignore[reportPrivateUsage]

        init_proactor._handle_completion(  # type: ignore[reportPrivateUsage]
            SimpleNamespace(request=cancellation, final=True, result=-errno.EINVAL)
        )

        assert contexts[0]["message"] == "Cancelling a native io_uring request failed"
        assert contexts[0]["exception"].errno == errno.EINVAL
    finally:
        loop.set_exception_handler(previous_handler)
        receiver.close()
        sender.close()
