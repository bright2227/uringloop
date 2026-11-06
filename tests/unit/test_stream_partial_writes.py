"""io_uring byte-count completion regressions, without kernel I/O.

Unlike the generic Phase 3 baseline, these completions deliberately acknowledge
only a prefix. The observable wire must contain each input byte exactly once.
"""

import errno
import socket

import pytest
from test_phase3_transport_contracts import FakeSocket, RecordingProtocol, TransportHarness

from uringloop.stream_transport import IoUringSocketTransport


class StreamHarness(TransportHarness):
    def make_transport(self, protocol=None, waiter=None):
        if protocol is None:
            protocol = RecordingProtocol()
        sock = FakeSocket()
        transport = IoUringSocketTransport(self.loop, sock, protocol, waiter)
        self.transports.append(transport)
        return transport, protocol, sock

    def complete_prefix(self, index, size):
        operation = self.proactor.writes[index]
        operation.future.set_result(size)
        self.drain()


@pytest.fixture
def stream_harness():
    harness = StreamHarness()
    try:
        yield harness
    finally:
        harness.close()


@pytest.mark.parametrize("sizes", [(1, 2, 3), (2, 2, 2), (5, 1)])
def test_partial_sends_retry_suffix_before_queued_writes(stream_harness, sizes):
    harness = stream_harness
    transport, _, _ = harness.make_transport()
    harness.drain()
    transport.write(b"abcdef")
    transport.write(b"XYZ")
    wire = bytearray()
    remaining = b"abcdef"

    for index, size in enumerate(sizes):
        assert harness.proactor.writes[index].data == remaining
        wire.extend(remaining[:size])
        remaining = remaining[size:]
        harness.complete_prefix(index, size)
        assert transport.get_write_buffer_size() == len(remaining) + 3

    assert not remaining
    assert harness.proactor.writes[-1].data == b"XYZ"
    wire.extend(harness.proactor.writes[-1].data)
    harness.complete_write(len(sizes))

    assert wire == b"abcdefXYZ"
    assert transport.get_write_buffer_size() == 0
    assert not harness.errors


def test_partial_send_retains_initial_and_queued_caller_snapshots(stream_harness):
    harness = stream_harness
    transport, _, _ = harness.make_transport()
    harness.drain()
    first = bytearray(b"first")
    second = bytearray(b"second")
    transport.write(memoryview(first))
    transport.write(second)
    first[:] = b"xxxxx"
    second[:] = b"yyyyyy"

    harness.complete_prefix(0, 2)
    assert harness.proactor.writes[1].data == b"rst"
    harness.complete_write(1)
    assert harness.proactor.writes[2].data == b"second"
    harness.complete_prefix(2, 3)
    assert harness.proactor.writes[3].data == b"ond"
    harness.complete_write(3)
    assert transport.get_write_buffer_size() == 0


def test_close_waits_for_partial_remainder_and_queued_data(stream_harness):
    harness = stream_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"first")
    transport.write(b"second")
    transport.close()
    harness.drain()

    assert harness.proactor.reads[0].future.cancelled()
    harness.complete_prefix(0, 2)
    assert harness.proactor.writes[1].data == b"rst"
    assert not sock.closed
    assert protocol.events == [("made",)]
    harness.complete_write(1)
    assert harness.proactor.writes[2].data == b"second"
    assert not sock.closed
    harness.complete_write(2)

    assert protocol.events == [("made",), ("lost", None)]
    assert sock.close_count == 1


def test_disconnected_socket_cleanup_detaches_server_despite_protocol_failure(stream_harness):
    harness = stream_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    detached = []

    class Server:
        def _detach(self):
            detached.append(True)

    def disconnected_shutdown(how):
        raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")

    def connection_lost(exc):
        protocol.events.append(("lost", exc))
        transport._call_connection_lost(exc)
        raise RuntimeError("protocol cleanup failed")

    sock.shutdown = disconnected_shutdown
    protocol.connection_lost = connection_lost
    transport._server = Server()
    transport.close()
    harness.drain()

    assert sock.close_count == 1
    assert transport._sock is None
    assert transport._server is None
    assert detached == [True]
    assert protocol.events.count(("lost", None)) == 1
    assert len(harness.errors) == 1
    assert str(harness.errors[0]["exception"]) == "protocol cleanup failed"


def test_write_eof_waits_for_partial_remainder_and_queued_data(stream_harness):
    harness = stream_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"first")
    transport.write(b"second")
    transport.write_eof()

    harness.complete_prefix(0, 1)
    assert not sock.shutdowns
    harness.complete_write(1)
    assert not sock.shutdowns
    harness.complete_prefix(2, 2)
    assert not sock.shutdowns
    harness.complete_write(3)

    assert sock.shutdowns == [socket.SHUT_WR]
    assert not transport.is_closing()
    harness.receive(b"still readable")
    assert protocol.events[-1] == ("data", b"still readable")


@pytest.mark.parametrize("completion_queued", [False, True])
def test_abort_ignores_late_write_callback_without_resubmission(stream_harness, completion_queued):
    harness = stream_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"first")
    transport.write(b"discarded")
    future = harness.proactor.writes[0].future
    if completion_queued:
        future.set_result(1)

    transport.abort()
    harness.drain()

    assert len(harness.proactor.writes) == 1
    assert transport.get_write_buffer_size() == 0
    assert protocol.events == [("made",), ("lost", None)]
    assert sock.close_count == 1
    assert not harness.errors


@pytest.mark.parametrize("size", [0, -1, 5, None, True])
def test_invalid_or_zero_progress_completion_closes_once(stream_harness, size):
    harness = stream_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"data")
    transport.write(b"discarded")

    harness.complete_prefix(0, size)
    transport.abort()
    harness.drain()

    assert transport.is_closing()
    assert len(harness.proactor.writes) == 1
    assert transport.get_write_buffer_size() == 0
    assert protocol.events[0] == ("made",)
    assert protocol.events[-1][0] == "lost"
    error = protocol.events[-1][1]
    assert isinstance(error, OSError)
    if type(size) is int and size == 0:
        assert isinstance(error, ConnectionResetError)
    else:
        assert error.errno == errno.EIO
    assert sum(event[0] == "lost" for event in protocol.events) == 1
    assert sock.close_count == 1
    assert not harness.errors


def test_cancelled_inflight_send_aborts_transport_without_restart(stream_harness):
    harness = stream_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"data")
    harness.proactor.writes[0].future.cancel()
    harness.drain()

    assert transport.is_closing()
    assert len(harness.proactor.writes) == 1
    assert isinstance(protocol.events[-1][1], ConnectionAbortedError)
    assert sock.close_count == 1
    assert not harness.errors


def test_cancelled_send_during_graceful_close_cannot_leave_transport_waiting(stream_harness):
    harness = stream_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"data")
    transport.write(b"queued")
    transport.close()
    harness.proactor.writes[0].future.cancel()
    harness.drain()

    assert len(harness.proactor.writes) == 1
    assert transport.get_write_buffer_size() == 0
    assert isinstance(protocol.events[-1][1], ConnectionAbortedError)
    assert sock.close_count == 1
    assert not harness.errors


def test_stale_completion_after_partial_retry_cannot_replace_current_send(stream_harness):
    harness = stream_harness
    transport, _, _ = harness.make_transport()
    harness.drain()
    transport.write(b"abcdef")
    old = harness.proactor.writes[0].future
    harness.complete_prefix(0, 2)
    current = harness.proactor.writes[1].future

    transport._loop_writing(old)

    assert transport._write_fut is current
    assert harness.proactor.writes[1].data == b"cdef"
    assert len(harness.proactor.writes) == 2
    assert transport.get_write_buffer_size() == 4
    harness.complete_write(1)


def test_synchronous_send_completions_retry_in_future_callback_snapshots(stream_harness, monkeypatch):
    harness = stream_harness
    transport, _, _ = harness.make_transport()
    harness.drain()
    original_send = harness.proactor.send

    def synchronous_send(sock, data):
        future = original_send(sock, data)
        future.set_result(1)
        return future

    monkeypatch.setattr(harness.proactor, "send", synchronous_send)
    transport.write(b"abcd")
    assert len(harness.proactor.writes) == 1
    harness.tick()
    assert len(harness.proactor.writes) == 2
    harness.drain()

    assert [operation.data for operation in harness.proactor.writes] == [b"abcd", b"bcd", b"cd", b"d"]
    assert transport.get_write_buffer_size() == 0
    assert not harness.errors


def test_retry_submission_error_closes_and_discards_queued_data(stream_harness, monkeypatch):
    harness = stream_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"data")
    transport.write(b"queued")
    error = OSError(errno.EPIPE, "retry failed")

    def fail_send(sock, data):
        raise error

    monkeypatch.setattr(harness.proactor, "send", fail_send)
    harness.complete_prefix(0, 2)

    assert protocol.events[-1] == ("lost", error)
    assert transport.get_write_buffer_size() == 0
    assert harness.proactor.reads[-1].future.cancelled()
    assert sock.close_count == 1
    assert not harness.errors


def test_partial_write_keeps_receive_side_independent(stream_harness):
    harness = stream_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    transport.write(b"abcdef")
    harness.complete_prefix(0, 2)
    harness.receive(b"incoming")

    assert protocol.events == [("made",), ("data", b"incoming")]
    assert transport.get_write_buffer_size() == 4
    assert len(harness.proactor.reads) == 2
    harness.complete_write(1)
    assert transport.get_write_buffer_size() == 0


def test_partial_progress_resumes_at_low_water_with_reentrant_write(stream_harness):
    harness = stream_harness

    class ReentrantProtocol(RecordingProtocol):
        def resume_writing(self):
            super().resume_writing()
            if len(harness.proactor.writes) == 2:
                self.transport.write(b"next")

    transport, protocol, _ = harness.make_transport(ReentrantProtocol())
    harness.drain()
    transport.set_write_buffer_limits(high=6, low=4)
    transport.write(b"abcdefgh")

    harness.complete_prefix(0, 4)
    assert protocol.events == [("made",), ("pause",), ("resume",), ("pause",)]
    assert harness.proactor.writes[1].data == b"efgh"
    assert transport.get_write_buffer_size() == 8
    harness.complete_write(1)
    assert harness.proactor.writes[2].data == b"next"
    assert protocol.events[-1] == ("resume",)
    harness.complete_write(2)

    assert transport.get_write_buffer_size() == 0
    assert sum(event[0] == "pause" for event in protocol.events) == 2
    assert sum(event[0] == "resume" for event in protocol.events) == 2
    assert not harness.errors


@pytest.mark.parametrize("action", ["close", "abort"])
def test_partial_progress_resume_callback_can_close_or_abort(stream_harness, action):
    harness = stream_harness

    class CloseOnResume(RecordingProtocol):
        def resume_writing(self):
            super().resume_writing()
            getattr(self.transport, action)()

    transport, protocol, sock = harness.make_transport(CloseOnResume())
    harness.drain()
    transport.set_write_buffer_limits(high=6, low=4)
    transport.write(b"abcdefgh")
    harness.complete_prefix(0, 4)

    assert transport.is_closing()
    assert harness.proactor.writes[1].data == b"efgh"
    if action == "close":
        assert not sock.closed
        harness.complete_write(1)
    else:
        assert harness.proactor.writes[1].future.cancelled()

    assert protocol.events == [("made",), ("pause",), ("resume",), ("lost", None)]
    assert len(harness.proactor.writes) == 2
    assert sock.close_count == 1
    assert not harness.errors


def test_sendfile_empty_waiter_waits_for_all_partial_bytes(stream_harness):
    harness = stream_harness
    transport, _, _ = harness.make_transport()
    harness.drain()
    transport.write(b"abcdef")
    waiter = transport._make_empty_waiter()

    harness.complete_prefix(0, 2)
    assert not waiter.done()
    with pytest.raises(RuntimeError, match="sendfile"):
        transport.write(b"blocked")
    harness.complete_write(1)

    assert waiter.done()
    assert waiter.result() is None
    transport._reset_empty_waiter()
    transport.write(b"allowed")
    harness.complete_write(2)


def test_pause_writing_can_reentrantly_abort_without_restart(stream_harness):
    harness = stream_harness

    class AbortOnPause(RecordingProtocol):
        def pause_writing(self):
            super().pause_writing()
            self.transport.abort()

    transport, protocol, sock = harness.make_transport(AbortOnPause())
    harness.drain()
    transport.set_write_buffer_limits(high=1, low=0)
    transport.write(b"data")
    harness.drain()

    assert harness.proactor.writes[0].future.cancelled()
    assert len(harness.proactor.writes) == 1
    assert protocol.events.count(("lost", None)) == 1
    assert transport.get_write_buffer_size() == 0
    assert sock.close_count == 1
    assert not harness.errors
