"""Phase 3's stream baseline, with no kernel or socket allocation.

These original tests exercise uringloop's socket-transport factory. Its current
implementation delegates to CPython's proactor transport; the harness preserves
real asyncio Future/Handle scheduling while replacing only the completion source.
The fake send operation completes the whole supplied buffer. Short kernel writes
need separate retry tests when the Phase 3 transport implementation is added.
"""

import asyncio
from asyncio import base_events
from dataclasses import dataclass
import socket

import pytest

from uringloop.loop import IoUringProactorEventLoop


class FakeSocket:
    family = socket.AF_UNIX
    type = socket.SOCK_STREAM
    proto = 0

    def __init__(self):
        self.closed = False
        self.close_count = 0
        self.shutdowns = []

    def fileno(self):
        return -1 if self.closed else 42

    def getsockname(self):
        return "local"

    def getpeername(self):
        return "peer"

    def shutdown(self, how):
        self.shutdowns.append(how)

    def close(self):
        self.closed = True
        self.close_count += 1


@dataclass
class ReadOperation:
    buffer: bytearray
    future: asyncio.Future


@dataclass
class WriteOperation:
    data: memoryview
    future: asyncio.Future


class FakeProactor:
    def __init__(self, loop):
        self.loop = loop
        self.reads = []
        self.writes = []

    def recv_into(self, sock, buffer):
        future = self.loop.create_future()
        self.reads.append(ReadOperation(buffer, future))
        return future

    def send(self, sock, data):
        future = self.loop.create_future()
        # Retain the caller's exported storage like the native proactor. A
        # fake-side copy would hide missing transport snapshots from tests.
        self.writes.append(WriteOperation(memoryview(data), future))
        return future


class RecordingProtocol(asyncio.Protocol):
    def __init__(self):
        self.events = []
        self.transport = None
        self.keep_open = False

    def connection_made(self, transport):
        self.transport = transport
        self.events.append(("made",))

    def data_received(self, data):
        self.events.append(("data", data))

    def eof_received(self):
        self.events.append(("eof",))
        return self.keep_open

    def connection_lost(self, exc):
        self.events.append(("lost", exc))

    def pause_writing(self):
        self.events.append(("pause",))

    def resume_writing(self):
        self.events.append(("resume",))


class TransportHarness:
    def __init__(self):
        # BaseEventLoop supplies actual callback contexts, exception handling,
        # and Futures without opening a selector's wakeup socketpair.
        self.loop = base_events.BaseEventLoop()
        self.proactor = FakeProactor(self.loop)
        self.loop._proactor = self.proactor
        self.errors = []
        self.loop.set_exception_handler(lambda loop, context: self.errors.append(context))
        self.transports = []

    def make_transport(self, protocol=None, waiter=None):
        if protocol is None:
            protocol = RecordingProtocol()
        sock = FakeSocket()
        transport = IoUringProactorEventLoop._make_socket_transport(self.loop, sock, protocol, waiter)
        self.transports.append(transport)
        return transport, protocol, sock

    def tick(self):
        # Use an asyncio ready snapshot: callbacks scheduled by this snapshot
        # belong to the next tick, just as they do in BaseEventLoop._run_once.
        for _ in range(len(self.loop._ready)):
            handle = self.loop._ready.popleft()
            if not handle.cancelled():
                handle._run()

    def drain(self):
        for _ in range(20):
            if not self.loop._ready:
                return
            self.tick()
        raise AssertionError("transport callbacks did not settle in 20 ready snapshots")

    def receive(self, data):
        operation = self.proactor.reads[-1]
        assert not operation.future.done()
        operation.buffer[: len(data)] = data
        operation.future.set_result(len(data))
        self.drain()

    def complete_write(self, index):
        operation = self.proactor.writes[index]
        operation.future.set_result(len(operation.data))
        self.drain()

    def close(self):
        for transport in self.transports:
            if not transport._called_connection_lost:
                transport.abort()
        self.drain()
        self.loop.close()


@pytest.fixture
def transport_harness():
    harness = TransportHarness()
    try:
        yield harness
    finally:
        harness.close()


def test_connection_made_precedes_waiter_and_first_read(transport_harness):
    harness = transport_harness
    waiter = harness.loop.create_future()
    transport, protocol, _ = harness.make_transport(waiter=waiter)
    observed = []
    waiter.add_done_callback(lambda future: observed.append(tuple(protocol.events)))

    assert protocol.events == []
    assert not waiter.done()
    assert not harness.proactor.reads
    harness.drain()

    assert observed == [(("made",),)]
    assert len(harness.proactor.reads) == 1
    assert protocol.transport is transport


def test_write_copies_mutable_data_and_preserves_queued_order(transport_harness):
    harness = transport_harness
    transport, _, _ = harness.make_transport()
    harness.drain()
    initial = bytearray(b"first")
    queued = bytearray(b"second")

    transport.write(initial)
    transport.write(memoryview(queued))
    initial[:] = b"xxxxx"
    queued[:] = b"yyyyyy"

    assert harness.proactor.writes[0].data == b"first"
    assert transport.get_write_buffer_size() == 11
    harness.complete_write(0)
    assert harness.proactor.writes[1].data == b"second"
    harness.complete_write(1)
    assert transport.get_write_buffer_size() == 0


def test_flow_control_counts_inflight_and_queued_bytes_and_pairs_callbacks(transport_harness):
    harness = transport_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    transport.set_write_buffer_limits(high=4, low=2)

    transport.write(b"1234")
    assert protocol.events == [("made",)]  # Equal to high does not pause.
    transport.write(b"56")
    transport.write(b"78")
    assert transport.get_write_buffer_size() == 8
    assert protocol.events == [("made",), ("pause",)]
    harness.complete_write(0)
    assert transport.get_write_buffer_size() == 4
    assert protocol.events == [("made",), ("pause",)]
    harness.complete_write(1)
    assert protocol.events == [("made",), ("pause",), ("resume",)]


def test_resume_writing_can_reentrantly_write_and_pause_again(transport_harness):
    harness = transport_harness

    class ReentrantProtocol(RecordingProtocol):
        def resume_writing(self):
            super().resume_writing()
            if len(harness.proactor.writes) == 1:
                self.transport.write(b"again")

    transport, protocol, _ = harness.make_transport(ReentrantProtocol())
    harness.drain()
    transport.set_write_buffer_limits(high=2, low=0)
    transport.write(b"first")
    harness.complete_write(0)

    assert harness.proactor.writes[1].data == b"again"
    assert transport.get_write_buffer_size() == 5
    assert protocol.events == [("made",), ("pause",), ("resume",), ("pause",)]
    harness.complete_write(1)
    assert protocol.events[-1] == ("resume",)


def test_close_cancels_read_but_flushes_writes_before_connection_lost(transport_harness):
    harness = transport_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    read = harness.proactor.reads[0].future
    transport.write(b"first")
    transport.write(b"second")

    transport.close()
    transport.close()
    harness.drain()
    assert read.cancelled()
    assert not harness.proactor.writes[0].future.cancelled()
    assert not sock.closed
    assert protocol.events == [("made",)]
    harness.complete_write(0)
    assert not sock.closed
    harness.complete_write(1)

    assert protocol.events == [("made",), ("lost", None)]
    assert sock.close_count == 1
    assert sock.shutdowns == [socket.SHUT_RDWR]
    assert not harness.errors


def test_abort_discards_queued_bytes_and_notifies_connection_lost_once(transport_harness):
    harness = transport_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"first")
    transport.write(b"discarded")

    transport.abort()
    transport.abort()
    transport.close()
    harness.drain()

    assert harness.proactor.writes[0].future.cancelled()
    assert harness.proactor.reads[0].future.cancelled()
    assert len(harness.proactor.writes) == 1
    assert transport.get_write_buffer_size() == 0
    assert protocol.events == [("made",), ("lost", None)]
    assert sock.close_count == 1
    assert not harness.errors


def test_write_eof_flushes_before_half_close_and_keeps_reading(transport_harness):
    harness = transport_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"last")
    transport.write_eof()
    transport.write_eof()

    assert transport.can_write_eof()
    assert not sock.shutdowns
    with pytest.raises(RuntimeError, match="write_eof"):
        transport.write(b"too late")
    harness.complete_write(0)

    assert sock.shutdowns == [socket.SHUT_WR]
    assert not transport.is_closing()
    harness.receive(b"peer reply")
    assert protocol.events == [("made",), ("data", b"peer reply")]


@pytest.mark.parametrize("keep_open", [False, True])
def test_peer_eof_is_delivered_once_and_protocol_controls_close(transport_harness, keep_open):
    harness = transport_harness
    transport, protocol, sock = harness.make_transport()
    protocol.keep_open = keep_open
    harness.drain()

    harness.receive(b"")
    harness.drain()

    assert len(harness.proactor.reads) == 1
    if keep_open:
        assert protocol.events == [("made",), ("eof",)]
        assert not sock.closed
        transport.write(b"reply after EOF")
        harness.complete_write(0)
    else:
        assert protocol.events == [("made",), ("eof",), ("lost", None)]
        assert sock.closed


def test_paused_read_preserves_completion_until_resume(transport_harness):
    harness = transport_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    pending_read = harness.proactor.reads[0].future

    transport.pause_reading()
    transport.pause_reading()
    assert not pending_read.cancelled()
    harness.receive(b"held")
    assert protocol.events == [("made",)]
    assert len(harness.proactor.reads) == 1
    transport.resume_reading()
    transport.resume_reading()
    harness.drain()

    assert protocol.events == [("made",), ("data", b"held")]
    assert len(harness.proactor.reads) == 2
    assert transport.is_reading()


def test_data_received_can_reentrantly_write_and_close(transport_harness):
    harness = transport_harness

    class ReplyAndClose(RecordingProtocol):
        def data_received(self, data):
            super().data_received(data)
            self.transport.write(b"reply")
            self.transport.close()

    transport, protocol, sock = harness.make_transport(ReplyAndClose())
    harness.drain()
    harness.receive(b"request")

    assert transport.is_closing()
    assert harness.proactor.reads[-1].future.cancelled()
    assert harness.proactor.writes[0].data == b"reply"
    assert protocol.events == [("made",), ("data", b"request")]
    harness.complete_write(0)
    assert protocol.events[-1] == ("lost", None)
    assert sock.close_count == 1
    assert not harness.errors
