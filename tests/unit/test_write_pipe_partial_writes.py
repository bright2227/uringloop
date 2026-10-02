"""Byte-count pipe-write contracts using deterministic Future completions."""

from dataclasses import dataclass

import pytest
from test_phase3_transport_contracts import FakeProactor, RecordingProtocol, TransportHarness

from uringloop.lib import POLLERR, POLLHUP
from uringloop.loop import IoUringProactorEventLoop, _IoUringWritePipeTransport


class FakePipe:
    """A pipe deliberately has no socket shutdown/getpeername methods."""

    def __init__(self):
        self.closed = False
        self.close_count = 0

    def fileno(self):
        return -1 if self.closed else 43

    def close(self):
        self.closed = True
        self.close_count += 1


@dataclass
class PollOperation:
    pipe: FakePipe
    mask: int
    future: object


class PipeProactor(FakeProactor):
    def __init__(self, loop):
        super().__init__(loop)
        self.polls = []

    def poll_add(self, pipe, mask):
        future = self.loop.create_future()
        self.polls.append(PollOperation(pipe, mask, future))
        return future


class PipeHarness(TransportHarness):
    def __init__(self):
        super().__init__()
        self.proactor = PipeProactor(self.loop)
        self.loop._proactor = self.proactor

    def make_transport(self, protocol=None, waiter=None):
        protocol = RecordingProtocol() if protocol is None else protocol
        pipe = FakePipe()
        transport = IoUringProactorEventLoop._make_write_pipe_transport(self.loop, pipe, protocol, waiter)
        self.transports.append(transport)
        return transport, protocol, pipe

    def complete_prefix(self, index, size):
        self.proactor.writes[index].future.set_result(size)
        self.drain()


@pytest.fixture
def pipe_harness():
    harness = PipeHarness()
    try:
        yield harness
    finally:
        harness.close()


def test_factory_registers_pipe_hup_watcher_and_retries_suffix_before_later_data(pipe_harness):
    harness = pipe_harness
    transport, protocol, pipe = harness.make_transport()
    harness.drain()

    assert isinstance(transport, _IoUringWritePipeTransport)
    assert transport.get_extra_info("pipe") is pipe
    assert not hasattr(pipe, "shutdown")
    assert protocol.events == [("made",)]
    assert len(harness.proactor.polls) == 1
    watcher = harness.proactor.polls[0]
    assert watcher.pipe is pipe
    assert watcher.mask == POLLERR | POLLHUP

    transport.write(b"abcdef")
    transport.write(b"XYZ")
    wire = bytearray()
    for index, expected, count in [(0, b"abcdef", 2), (1, b"cdef", 1), (2, b"def", 3), (3, b"XYZ", 3)]:
        operation = harness.proactor.writes[index]
        assert operation.data == expected
        wire.extend(operation.data[:count])
        harness.complete_prefix(index, count)

    assert wire == b"abcdefXYZ"
    assert transport.get_write_buffer_size() == 0
    assert not watcher.future.done()
    assert not harness.errors


@pytest.mark.parametrize("close_method", ["close", "write_eof"])
def test_pipe_close_waits_for_partial_suffix_and_queued_writes_without_shutdown(pipe_harness, close_method):
    harness = pipe_harness
    transport, protocol, pipe = harness.make_transport()
    harness.drain()
    transport.write(b"first")
    transport.write(b"second")

    getattr(transport, close_method)()
    harness.drain()
    assert harness.proactor.polls[0].future.cancelled()
    assert not pipe.closed
    harness.complete_prefix(0, 2)
    assert harness.proactor.writes[1].data == b"rst"
    assert not pipe.closed
    harness.complete_write(1)
    assert harness.proactor.writes[2].data == b"second"
    assert not pipe.closed
    harness.complete_write(2)
    transport.close()
    harness.drain()

    assert protocol.events == [("made",), ("lost", None)]
    assert pipe.close_count == 1
    assert transport.get_write_buffer_size() == 0
    assert not harness.errors


@pytest.mark.parametrize("completion_queued", [False, True])
def test_pipe_abort_ignores_late_send_callback_and_discards_queue(pipe_harness, completion_queued):
    harness = pipe_harness
    transport, protocol, pipe = harness.make_transport()
    harness.drain()
    transport.write(b"first")
    transport.write(b"discarded")
    pending = harness.proactor.writes[0].future
    if completion_queued:
        pending.set_result(1)

    transport.abort()
    transport.abort()
    harness.drain()

    assert len(harness.proactor.writes) == 1
    assert harness.proactor.polls[0].future.cancelled()
    assert pending.done()
    assert transport.get_write_buffer_size() == 0
    assert protocol.events == [("made",), ("lost", None)]
    assert pipe.close_count == 1
    assert not harness.errors


@pytest.mark.parametrize("mask", [POLLHUP, POLLERR, POLLHUP | POLLERR])
def test_pipe_hup_with_pending_write_aborts_and_reports_broken_pipe_once(pipe_harness, mask):
    harness = pipe_harness
    transport, protocol, pipe = harness.make_transport()
    harness.drain()
    transport.write(b"first")
    transport.write(b"discarded")
    pending = harness.proactor.writes[0].future
    harness.proactor.polls[0].future.set_result(mask)
    harness.drain()
    transport.abort()
    harness.drain()

    assert pending.cancelled()
    assert len(harness.proactor.writes) == 1
    assert transport.get_write_buffer_size() == 0
    assert protocol.events[0] == ("made",)
    assert len(protocol.events) == 2
    assert protocol.events[-1][0] == "lost"
    assert isinstance(protocol.events[-1][1], BrokenPipeError)
    assert pipe.close_count == 1
    assert not harness.errors


def test_pipe_hup_without_pending_write_closes_normally(pipe_harness):
    harness = pipe_harness
    transport, protocol, pipe = harness.make_transport()
    harness.drain()
    harness.proactor.polls[0].future.set_result(POLLHUP)
    harness.drain()

    assert transport.is_closing()
    assert protocol.events == [("made",), ("lost", None)]
    assert pipe.close_count == 1
    assert not harness.errors
