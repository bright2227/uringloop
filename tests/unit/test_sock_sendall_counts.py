"""The public socket API shares the transport's counted-send safety rules."""

import errno

import pytest
from test_phase3_transport_contracts import FakeSocket, TransportHarness

from uringloop.loop import IoUringProactorEventLoop


@pytest.fixture
def harness():
    value = TransportHarness()
    try:
        yield value
    finally:
        value.close()


def start_send(harness, data):
    task = harness.loop.create_task(IoUringProactorEventLoop.sock_sendall(harness.loop, FakeSocket(), data))
    harness.drain()
    return task


def test_sock_sendall_retries_only_unsent_suffix(harness):
    task = start_send(harness, b"abcdef")
    for index, (payload, count) in enumerate([(b"abcdef", 2), (b"cdef", 1), (b"def", 3)]):
        operation = harness.proactor.writes[index]
        assert operation.data == payload
        operation.future.set_result(count)
        harness.drain()
    assert task.done()
    assert task.result() is None


@pytest.mark.parametrize("count", [0, -1, 7, None, True])
def test_sock_sendall_rejects_invalid_counts_without_spinning(harness, count):
    task = start_send(harness, b"abcdef")
    harness.proactor.writes[0].future.set_result(count)
    harness.drain()
    assert len(harness.proactor.writes) == 1
    with pytest.raises(OSError) as raised:
        task.result()
    if type(count) is int and count == 0:
        assert isinstance(raised.value, ConnectionResetError)
    else:
        assert raised.value.errno == errno.EIO


def test_sock_sendall_empty_payload_does_not_submit(harness):
    task = start_send(harness, b"")
    assert task.result() is None
    assert harness.proactor.writes == []
