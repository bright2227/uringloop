"""Native one-shot transport contracts without allocating a ring or sockets.

The fake proactor retains exported buffers and callback ownership until a final
CQE, including cancelled requests. Protocol delivery uses real asyncio ready
snapshots so constructor contexts and reentrant callbacks remain observable.
"""

import asyncio
from asyncio import base_events
import contextvars
from dataclasses import dataclass
import errno
import gc
import inspect
import socket
import sys
from types import MethodType, SimpleNamespace
import weakref

import pytest
from test_phase3_transport_contracts import FakeSocket, RecordingProtocol, TransportHarness

from uringloop.loop import IoUringProactorEventLoop
from uringloop.native_stream import NativeStreamTransport
from uringloop.stream_transport import IoUringSocketTransport


@dataclass(eq=False)
class FakeRequest:
    socket: FakeSocket
    buffer: memoryview
    callback: object
    completed: bool = False
    cancelled: bool = False


class NativeProactor:
    def __init__(self):
        self.reads = []
        self.writes = []
        self.cancellations = []
        self.transports = weakref.WeakSet()

    def register_transport(self, transport):
        self.transports.add(transport)

    def recv_transport(self, sock, buffer, callback):
        request = FakeRequest(sock, memoryview(buffer), callback)
        assert not request.buffer.readonly
        self.reads.append(request)
        return request

    def send_transport(self, sock, buffer, callback):
        request = FakeRequest(sock, memoryview(buffer), callback)
        self.writes.append(request)
        return request

    def cancel_transport(self, request):
        request.cancelled = True
        self.cancellations.append(request)


class NativeHarness:
    def __init__(self):
        self.loop = base_events.BaseEventLoop()
        self.proactor = NativeProactor()
        self.loop._proactor = self.proactor
        self.loop._transport_mode = "native"
        self.loop._native_server_attach = MethodType(IoUringProactorEventLoop._native_server_attach, self.loop)
        self.loop._native_server_detach = MethodType(IoUringProactorEventLoop._native_server_detach, self.loop)
        self.errors = []
        self.loop.set_exception_handler(lambda loop, context: self.errors.append(context))
        self.transports = []

    def make_transport(self, protocol=None, waiter=None, *, server=None, extra=None):
        protocol = RecordingProtocol() if protocol is None else protocol
        sock = FakeSocket()
        transport = IoUringProactorEventLoop._make_socket_transport(self.loop, sock, protocol, waiter, extra, server)
        self.transports.append(transport)
        return transport, protocol, sock

    def drain(self):
        for _ in range(30):
            if not self.loop._ready:
                return
            for _ in range(len(self.loop._ready)):
                handle = self.loop._ready.popleft()
                if not handle.cancelled():
                    handle._run()
        raise AssertionError("native transport ready callbacks did not settle")

    def complete(self, request, result):
        assert not request.completed
        request.completed = True
        callback = request.callback
        try:
            callback(SimpleNamespace(request=request, result=result, final=True))
        finally:
            # Match proactor retirement: historical logs retain the buffer and
            # socket, but not a bound callback that would artificially leak C.
            request.callback = None
        self.drain()

    def receive(self, data):
        request = self.proactor.reads[-1]
        request.buffer[: len(data)] = data
        self.complete(request, len(data))

    def complete_write(self, index, count=None):
        request = self.proactor.writes[index]
        self.complete(request, len(request.buffer) if count is None else count)

    def close(self):
        for transport in self.transports:
            transport.abort()
        self.drain()
        for request in [*self.proactor.reads, *self.proactor.writes]:
            if not request.completed:
                self.complete(request, -errno.ECANCELED)
        self.drain()
        self.loop.close()


@pytest.fixture
def native_harness():
    harness = NativeHarness()
    try:
        yield harness
    finally:
        harness.close()


def test_native_constructor_defers_connection_made_then_resolves_waiter_and_starts_read(native_harness):
    harness = native_harness
    waiter = harness.loop.create_future()
    transport, protocol, sock = harness.make_transport(waiter=waiter, extra={"token": "value"})
    observed = []
    waiter.add_done_callback(lambda future: observed.append(tuple(protocol.events)))

    assert isinstance(transport, NativeStreamTransport)
    assert isinstance(transport, asyncio.Transport)
    assert not transport._start_tls_compatible
    assert not waiter.done()
    assert not protocol.events
    assert not harness.proactor.reads
    assert transport in harness.proactor.transports
    assert transport.get_extra_info("token") == "value"
    assert transport.get_extra_info("socket").fileno() == sock.fileno()
    harness.drain()

    assert waiter.result() is None
    assert observed == [(("made",),)]
    assert protocol.transport is transport
    assert len(harness.proactor.reads) == 1
    assert harness.proactor.reads[0].socket is sock


def test_native_write_snapshots_caller_storage_and_retries_before_queued_bytes(native_harness):
    harness = native_harness
    transport, _, _ = harness.make_transport()
    harness.drain()
    first, queued = bytearray(b"abcdef"), bytearray(b"XYZ")
    transport.write(first)
    transport.write(memoryview(queued))
    first[:] = b"xxxxxx"
    queued[:] = b"yyy"
    assert transport.get_write_buffer_size() == 9
    harness.drain()
    wire = bytearray()

    for index, payload, size in [(0, b"abcdef", 2), (1, b"cdef", 1), (2, b"def", 3), (3, b"XYZ", 3)]:
        request = harness.proactor.writes[index]
        assert request.buffer == payload
        wire.extend(request.buffer[:size])
        harness.complete_write(index, size)

    assert wire == b"abcdefXYZ"
    assert transport.get_write_buffer_size() == 0
    assert not harness.errors


@pytest.mark.parametrize("count", [0, -errno.EPIPE, 8])
def test_native_bad_write_completion_closes_without_retrying_or_losing_exception(native_harness, count):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"data")
    transport.write(b"discarded")
    harness.drain()
    harness.complete_write(0, count)

    assert transport.is_closing()
    assert len(harness.proactor.writes) == 1
    assert transport.get_write_buffer_size() == 0
    assert protocol.events[-1][0] == "lost"
    assert isinstance(protocol.events[-1][1], OSError)
    if count == -errno.EPIPE:
        assert protocol.events[-1][1].errno == errno.EPIPE
    assert sock.close_count == 1


@pytest.mark.parametrize("count", [True, None, 1 << 100])
def test_native_malformed_matched_completion_aborts_and_reports_owner_without_hanging(native_harness, count):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"data")
    transport.write(b"discarded")
    harness.drain()
    harness.complete(harness.proactor.writes[0], count)

    assert transport.is_closing()
    assert transport.get_write_buffer_size() == 0
    assert len(harness.proactor.writes) == 1
    assert protocol.events[-1][0] == "lost"
    assert isinstance(protocol.events[-1][1], Exception)
    assert sock.close_count == 1
    assert len(harness.errors) == 1
    assert harness.errors[0]["transport"] is transport
    assert harness.errors[0]["protocol"] is protocol


def test_native_stale_wrong_request_ignores_malformed_completion_without_reading_result(native_harness):
    harness = native_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    transport.write(b"data")
    harness.drain()
    request = harness.proactor.writes[0]

    class Stale:
        request = object()

        @property
        def result(self):
            raise AssertionError("wrong-request result must not be inspected")

    request.callback(Stale())
    harness.drain()
    assert len(harness.proactor.writes) == 1
    assert transport.get_write_buffer_size() == 4
    assert protocol.events == [("made",)]
    assert not harness.errors
    harness.complete_write(0)
    assert transport.get_write_buffer_size() == 0


@pytest.mark.parametrize("operation", ["read", "write"])
def test_native_cqe_state_update_defers_protocol_delivery_until_ready_snapshot(native_harness, operation):
    harness = native_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    if operation == "read":
        request = harness.proactor.reads[0]
        request.buffer[:1] = b"x"
        before = [("made",)]
    else:
        transport.set_write_buffer_limits(high=2, low=0)
        transport.write(b"x")
        transport.write(b"xx")
        harness.drain()
        request = harness.proactor.writes[0]
        before = [("made",), ("pause",)]
    callback = request.callback
    request.completed = True
    try:
        callback(SimpleNamespace(request=request, result=len(request.buffer) if operation == "write" else 1, final=True))
    finally:
        request.callback = None
    assert protocol.events == before
    harness.drain()
    if operation == "read":
        assert protocol.events == [("made",), ("data", b"x")]
    else:
        # The second queued write still lies ahead of low=0, so finish it to
        # observe resume delivery only through the later ready snapshot.
        harness.complete_write(1)
        assert protocol.events == [("made",), ("pause",), ("resume",)]


@pytest.mark.parametrize("retry_errno", [errno.EAGAIN, errno.EINTR])
def test_native_retryable_write_error_retains_full_payload_ahead_of_queued_data(native_harness, retry_errno):
    harness = native_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    transport.write(b"first")
    transport.write(b"second")
    harness.drain()
    harness.complete_write(0, -retry_errno)

    assert harness.proactor.writes[1].buffer == b"first"
    assert transport.get_write_buffer_size() == 11
    assert protocol.events == [("made",)]
    harness.complete_write(1)
    assert harness.proactor.writes[2].buffer == b"second"
    harness.complete_write(2)
    assert transport.get_write_buffer_size() == 0
    assert not harness.errors


def test_native_terminal_read_error_closes_once_and_preserves_errno(native_harness):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    harness.complete(harness.proactor.reads[0], -errno.ECONNRESET)
    transport.abort()
    harness.drain()

    assert len(harness.proactor.reads) == 1
    assert protocol.events[0] == ("made",)
    assert protocol.events[-1][0] == "lost"
    assert protocol.events[-1][1].errno == errno.ECONNRESET
    assert sum(event[0] == "lost" for event in protocol.events) == 1
    assert sock.close_count == 1


def test_native_watermarks_count_unsent_prefix_and_queue_and_pair_flow_callbacks(native_harness):
    harness = native_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    transport.set_write_buffer_limits(high=4, low=2)
    assert transport.get_write_buffer_limits() == (2, 4)
    transport.write(b"1234")
    assert protocol.events == [("made",)]
    transport.write(b"56")
    transport.write(b"78")
    harness.drain()
    assert transport.get_write_buffer_size() == 8
    assert protocol.events == [("made",), ("pause",)]
    harness.complete_write(0, 2)
    assert transport.get_write_buffer_size() == 6
    harness.complete_write(1)
    assert transport.get_write_buffer_size() == 4
    assert protocol.events == [("made",), ("pause",)]
    harness.complete_write(2, 2)
    assert transport.get_write_buffer_size() == 2
    assert protocol.events == [("made",), ("pause",), ("resume",)]
    harness.complete_write(3)
    assert transport.get_write_buffer_size() == 0


def test_native_graceful_close_cancels_read_but_flushes_partial_and_queued_writes(native_harness):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    read = harness.proactor.reads[0]
    transport.write(b"first")
    transport.write(b"second")
    transport.close()
    transport.close()
    harness.drain()
    assert read.cancelled
    assert not harness.proactor.writes[0].cancelled
    assert not sock.closed
    harness.complete_write(0, 2)
    assert harness.proactor.writes[1].buffer == b"rst"
    harness.complete_write(1)
    assert harness.proactor.writes[2].buffer == b"second"
    assert not sock.closed
    harness.complete_write(2)

    assert protocol.events == [("made",), ("lost", None)]
    assert sock.close_count == 1


def test_native_abort_keeps_cancelled_request_ownership_until_late_final_cqe(native_harness):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    read = harness.proactor.reads[0]
    transport.write(b"first")
    transport.write(b"discarded")
    harness.drain()
    write = harness.proactor.writes[0]
    transport.abort()
    transport.abort()
    harness.drain()
    assert read.cancelled and write.cancelled
    assert write.buffer == b"first"
    assert write.socket is sock
    harness.complete(write, 1)
    harness.complete(read, -errno.ECANCELED)

    assert len(harness.proactor.writes) == 1
    assert len(harness.proactor.reads) == 1
    assert transport.get_write_buffer_size() == 0
    assert protocol.events == [("made",), ("lost", None)]
    assert sock.close_count == 1
    assert not harness.errors


def test_native_paused_read_retains_one_completion_without_submitting_more(native_harness):
    harness = native_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    transport.pause_reading()
    transport.pause_reading()
    assert not transport.is_reading()
    assert not harness.proactor.reads[0].cancelled
    harness.receive(b"held")
    assert protocol.events == [("made",)]
    assert len(harness.proactor.reads) == 1
    transport.resume_reading()
    transport.resume_reading()
    harness.drain()

    assert protocol.events == [("made",), ("data", b"held")]
    assert len(harness.proactor.reads) == 2
    assert transport.is_reading()


def test_native_write_eof_flushes_before_half_close_and_keeps_receiving(native_harness):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"last")
    harness.drain()
    transport.write_eof()
    transport.write_eof()
    assert transport.can_write_eof()
    assert not sock.shutdowns
    with pytest.raises(RuntimeError, match="write_eof"):
        transport.write(b"late")
    harness.complete_write(0, 2)
    assert not sock.shutdowns
    harness.complete_write(1)

    assert sock.shutdowns == [socket.SHUT_WR]
    assert not transport.is_closing()
    harness.receive(b"peer reply")
    assert protocol.events[-1] == ("data", b"peer reply")


@pytest.mark.parametrize("keep_open", [False, True])
def test_native_peer_eof_is_delivered_once_and_protocol_controls_close(native_harness, keep_open):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    protocol.keep_open = keep_open
    harness.drain()
    harness.receive(b"")
    assert len(harness.proactor.reads) == 1
    if keep_open:
        assert protocol.events == [("made",), ("eof",)]
        assert not sock.closed
        transport.write(b"reply")
        harness.drain()
        harness.complete_write(0)
    else:
        assert protocol.events == [("made",), ("eof",), ("lost", None)]
        assert sock.close_count == 1


def test_native_protocol_callbacks_keep_constructor_context_after_replacement(native_harness):
    harness = native_harness
    marker = contextvars.ContextVar("native_transport_context", default="unset")
    seen = []

    class ContextProtocol(RecordingProtocol):
        def connection_made(self, transport):
            seen.append(("made", marker.get()))
            super().connection_made(transport)

        def data_received(self, data):
            seen.append(("data", marker.get(), data))
            super().data_received(data)

        def connection_lost(self, exc):
            seen.append(("lost", marker.get()))
            super().connection_lost(exc)

    token = marker.set("constructor")
    try:
        transport, old, _ = harness.make_transport(ContextProtocol())
        marker.set("completion driver")
        harness.drain()
        harness.receive(b"old")
        replacement = ContextProtocol()
        transport.set_protocol(replacement)
        assert transport.get_protocol() is replacement
        harness.receive(b"new")
        transport.abort()
        harness.drain()
    finally:
        marker.reset(token)

    assert old.events == [("made",), ("data", b"old")]
    assert replacement.events == [("data", b"new"), ("lost", None)]
    assert seen == [
        ("made", "constructor"),
        ("data", "constructor", b"old"),
        ("data", "constructor", b"new"),
        ("lost", "constructor"),
    ]


def test_native_receive_callback_can_write_and_close_without_another_receive(native_harness):
    harness = native_harness

    class ReplyAndClose(RecordingProtocol):
        def data_received(self, data):
            super().data_received(data)
            self.transport.write(b"reply")
            self.transport.close()

    transport, protocol, sock = harness.make_transport(ReplyAndClose())
    harness.drain()
    harness.receive(b"request")
    assert transport.is_closing()
    assert len(harness.proactor.reads) == 1
    assert harness.proactor.writes[0].buffer == b"reply"
    harness.complete_write(0)
    assert protocol.events == [("made",), ("data", b"request"), ("lost", None)]
    assert sock.close_count == 1
    assert not harness.errors


def test_native_resume_writing_can_reentrantly_write_and_pause_again(native_harness):
    harness = native_harness

    class Reentrant(RecordingProtocol):
        def resume_writing(self):
            super().resume_writing()
            if len(harness.proactor.writes) == 1:
                self.transport.write(b"again")

    transport, protocol, _ = harness.make_transport(Reentrant())
    harness.drain()
    transport.set_write_buffer_limits(high=2, low=0)
    transport.write(b"first")
    harness.drain()
    harness.complete_write(0)
    assert harness.proactor.writes[1].buffer == b"again"
    assert transport.get_write_buffer_size() == 5
    assert protocol.events == [("made",), ("pause",), ("resume",), ("pause",)]
    harness.complete_write(1)
    assert protocol.events[-1] == ("resume",)


def test_native_callback_exception_preserves_identity_and_reports_transport_context(native_harness):
    harness = native_harness
    error = RuntimeError("data callback failed")

    class Broken(RecordingProtocol):
        def data_received(self, data):
            raise error

    transport, protocol, sock = harness.make_transport(Broken())
    harness.drain()
    harness.receive(b"data")

    assert len(harness.errors) == 1
    context = harness.errors[0]
    assert context["exception"] is error
    assert context["transport"] is transport
    assert context["protocol"] is protocol
    assert protocol.events == [("made",), ("lost", error)]
    assert sock.close_count == 1


def test_native_server_attach_detach_are_paired_and_version_correct(native_harness):
    harness = native_harness

    class Server:
        def __init__(self):
            self.attached = []
            self.detached = []

        def _attach(self, *args):
            self.attached.append(args)

        def _detach(self, *args):
            self.detached.append(args)

    server = Server()
    transport, _, sock = harness.make_transport(server=server)
    harness.drain()
    expected = (transport,) if sys.version_info >= (3, 13) else ()
    assert server.attached == [expected]
    transport.abort()
    transport.abort()
    harness.drain()
    assert server.detached == [expected]
    assert sock.close_count == 1


def test_native_gc_collects_protocol_transport_cycles_after_request_retirement(native_harness):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.abort()
    harness.drain()
    harness.complete(harness.proactor.reads[0], -errno.ECANCELED)
    assert sock.close_count == 1
    protocol_reference, transport_reference = weakref.ref(protocol), weakref.ref(transport)
    harness.transports.clear()
    del transport, protocol
    gc.collect()
    assert protocol_reference() is None
    assert transport_reference() is None
    assert not harness.proactor.transports


def test_native_gc_detaches_server_for_paused_unreachable_cycle_without_active_requests(native_harness):
    harness = native_harness

    class Server:
        def __init__(self):
            self.attach_count = self.detach_count = 0

        def _attach(self, *args):
            self.attach_count += 1

        def _detach(self, *args):
            self.detach_count += 1

    server = Server()
    transport, protocol, sock = harness.make_transport(server=server)
    transport.pause_reading()
    harness.drain()
    assert not harness.proactor.reads
    assert not harness.proactor.writes
    assert server.attach_count == 1
    assert server.detach_count == 0
    reference = weakref.ref(transport)
    harness.transports.clear()
    del transport, protocol
    gc.collect()

    assert reference() is None
    assert server.detach_count == 1
    assert sock.close_count == 1


@pytest.mark.parametrize("writing_paused", [False, True])
def test_native_transport_supports_sendfile_fallback_protocol_and_restoration(native_harness, writing_paused):
    harness = native_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    if writing_paused:
        transport.set_write_buffer_limits(high=2, low=0)
        transport.write(b"pending")
        harness.drain()
    assert transport._protocol_paused == writing_paused

    fallback = base_events._SendfileFallbackProtocol(transport)
    assert transport.get_protocol() is fallback
    assert not transport.is_reading()
    waiter = harness.loop.create_task(fallback.drain())
    harness.drain()
    if writing_paused:
        assert not waiter.done()
        harness.complete_write(0)
    assert waiter.result() is None
    restore = harness.loop.create_task(fallback.restore())
    harness.drain()

    assert restore.result() is None
    assert transport.get_protocol() is protocol
    assert transport.is_reading()
    assert not transport._protocol_paused
    harness.receive(b"after fallback")
    assert protocol.events[-1] == ("data", b"after fallback")
    assert not harness.errors


def test_native_factory_closes_socket_once_after_registration_failure_and_detaches_server(native_harness, monkeypatch):
    harness = native_harness
    error = RuntimeError("registration failed")
    sock = FakeSocket()
    attached, detached = [], []
    server = SimpleNamespace(_attach=lambda *args: attached.append(args), _detach=lambda *args: detached.append(args))

    def fail_registration(transport):
        raise error

    monkeypatch.setattr(harness.proactor, "register_transport", fail_registration)
    with pytest.raises(RuntimeError) as raised:
        IoUringProactorEventLoop._make_socket_transport(harness.loop, sock, RecordingProtocol(), server=server)

    assert raised.value is error
    assert sock.close_count == 1
    assert len(attached) == len(detached) == 1
    assert attached[0] == detached[0]
    assert not harness.proactor.reads
    assert not harness.proactor.writes
    harness.drain()


def test_native_factory_keeps_python_default_and_buffered_or_ssl_protocol_fallback():
    assert inspect.signature(IoUringProactorEventLoop).parameters["transports"].default == "python"
    harness = TransportHarness()
    try:
        transport, _, _ = harness.make_transport()
        assert isinstance(transport, IoUringSocketTransport)
        harness.loop._transport_mode = "native"

        class Buffered(asyncio.BufferedProtocol):
            def get_buffer(self, sizehint):
                return bytearray(16)

            def buffer_updated(self, nbytes):
                pass

        buffered, _, _ = harness.make_transport(Buffered())
        assert isinstance(buffered, IoUringSocketTransport)
        assert not isinstance(buffered, NativeStreamTransport)
        # asyncio's initial SSLProtocol also implements BufferedProtocol, so
        # it follows this factory path rather than entering the native core.
        from asyncio import sslproto

        assert issubclass(sslproto.SSLProtocol, asyncio.BufferedProtocol)
        harness.drain()
    finally:
        harness.close()
