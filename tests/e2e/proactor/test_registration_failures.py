"""Failed Python registration must never submit the caller's fd operation."""

from asyncio import base_events
import select
import socket
import tempfile
import time

import pytest

from uringloop import native_proactor
from uringloop.native_proactor import NativeIoUringProactor


@pytest.mark.parametrize("operation", ["send", "file_write", "poll", "readiness"])
@pytest.mark.parametrize("failure", ["future", "record", "cache", "cache_after_insert"])
def test_failed_registration_cannot_escape_in_later_submission(operation, failure, monkeypatch):
    loop = base_events.BaseEventLoop()
    proactor = NativeIoUringProactor()
    proactor.set_loop(loop)
    left, right = socket.socketpair()
    left.setblocking(False)
    right.settimeout(1)
    error = MemoryError("injected allocation failure")

    def fail(*args, **kwargs):
        raise error

    class FailingCache(dict):
        def __setitem__(self, key, value):
            if failure == "cache_after_insert":
                super().__setitem__(key, value)
            raise error

    try:
        with tempfile.TemporaryFile() as file:
            cache = proactor._cache
            with monkeypatch.context() as patch:
                if failure == "future":
                    patch.setattr(proactor, "_new_future", fail)
                elif failure == "record":
                    patch.setattr(native_proactor, "_Pending", fail)
                else:
                    patch.setattr(proactor, "_cache", FailingCache())

                with pytest.raises(MemoryError) as caught:
                    if operation == "send":
                        proactor.send(left, b"failed-operation:")
                    elif operation == "file_write":
                        proactor.send(file, b"failed-operation:")
                    elif operation == "poll":
                        proactor.poll_add(left, select.POLLIN)
                    else:
                        proactor.recvfrom(left, 1)
                assert caught.value is error
                assert not proactor._cache
                assert proactor._ring.pending == (0 if failure == "future" else 1)

            assert proactor._cache is cache
            successful = proactor.send(file if operation == "file_write" else left, b"successful-operation")
            deadline = time.monotonic() + 1
            while not successful.done() and time.monotonic() < deadline:
                proactor._poll(0.05)
            assert successful.result() == len(b"successful-operation")
            # Retire every NOP as well as the marker. No poll/read request may
            # remain unregistered and no failed send/write may reach the wire.
            proactor._poll(0)
            assert proactor._ring.pending == 0
            assert not proactor._cache
            if operation == "file_write":
                file.seek(0)
                assert file.read() == b"successful-operation"
            else:
                assert right.recv(1024) == b"successful-operation"
    finally:
        proactor.close()
        left.close()
        right.close()
        loop.close()
