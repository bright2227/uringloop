"""BufferedProtocol/error/context baseline for the Phase 3 transport work.

These are original contract tests against uringloop's current transport factory,
using the deterministic asyncio scheduling harness from the stream baseline.
"""

import asyncio
import contextvars

import pytest
from test_phase3_transport_contracts import TransportHarness


@pytest.fixture
def transport_harness():
    harness = TransportHarness()
    try:
        yield harness
    finally:
        harness.close()


class RecordingBufferedProtocol(asyncio.BufferedProtocol):
    def __init__(self, capacity=4):
        self.buffer = bytearray(capacity)
        self.events = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        self.events.append(("made",))

    def get_buffer(self, sizehint):
        self.events.append(("get", sizehint))
        return self.buffer

    def buffer_updated(self, nbytes):
        self.events.append(("updated", bytes(self.buffer[:nbytes])))

    def eof_received(self):
        self.events.append(("eof",))
        return False

    def connection_lost(self, exc):
        self.events.append(("lost", exc))


def test_buffered_protocol_splits_payload_without_loss_and_skips_buffer_for_eof(transport_harness):
    harness = transport_harness
    _, protocol, _ = harness.make_transport(RecordingBufferedProtocol(capacity=3))
    harness.drain()
    harness.receive(b"abcdefgh")
    harness.receive(b"")

    assert protocol.events == [
        ("made",),
        ("get", 8),
        ("updated", b"abc"),
        ("get", 5),
        ("updated", b"def"),
        ("get", 2),
        ("updated", b"gh"),
        ("eof",),
        ("lost", None),
    ]
    assert len(harness.proactor.reads) == 2
    assert not harness.errors


@pytest.mark.parametrize("failure", ["get_buffer", "empty_buffer", "buffer_updated"])
def test_buffered_protocol_failure_reports_context_and_closes_once(transport_harness, failure):
    harness = transport_harness
    error = RuntimeError("protocol callback failed")

    class BrokenProtocol(RecordingBufferedProtocol):
        def get_buffer(self, sizehint):
            if failure == "get_buffer":
                raise error
            if failure == "empty_buffer":
                return bytearray()
            return super().get_buffer(sizehint)

        def buffer_updated(self, nbytes):
            if failure == "buffer_updated":
                raise error
            super().buffer_updated(nbytes)

    transport, protocol, sock = harness.make_transport(BrokenProtocol())
    harness.drain()
    harness.receive(b"data")
    transport.abort()
    harness.drain()

    assert len(harness.errors) == 1
    context = harness.errors[0]
    assert context["transport"] is transport
    assert context["protocol"] is protocol
    if failure == "empty_buffer":
        assert isinstance(context["exception"], RuntimeError)
        assert "empty buffer" in str(context["exception"])
    else:
        assert context["exception"] is error
    assert "protocol.buffer_updated()" in context["message"]
    assert protocol.events[-1] == ("lost", context["exception"])
    assert sum(event[0] == "lost" for event in protocol.events) == 1
    assert harness.proactor.reads[-1].future.cancelled()
    assert sock.close_count == 1


def test_buffered_callbacks_retain_registration_context_across_completions(transport_harness):
    harness = transport_harness
    marker = contextvars.ContextVar("phase3_protocol_context", default="unset")
    seen = []

    class ContextProtocol(RecordingBufferedProtocol):
        def connection_made(self, transport):
            seen.append(("made", marker.get()))
            super().connection_made(transport)

        def get_buffer(self, sizehint):
            seen.append(("get", marker.get()))
            return super().get_buffer(sizehint)

        def buffer_updated(self, nbytes):
            seen.append(("updated", marker.get()))
            super().buffer_updated(nbytes)

        def connection_lost(self, exc):
            seen.append(("lost", marker.get()))
            super().connection_lost(exc)

    token = marker.set("constructor")
    try:
        harness.make_transport(ContextProtocol())
        marker.set("completion driver")
        harness.drain()
        harness.receive(b"one")
        harness.receive(b"two")
        harness.receive(b"")
    finally:
        marker.reset(token)

    assert seen == [
        ("made", "constructor"),
        ("get", "constructor"),
        ("updated", "constructor"),
        ("get", "constructor"),
        ("updated", "constructor"),
        ("lost", "constructor"),
    ]


def test_buffer_updated_can_reentrantly_write_and_abort(transport_harness):
    harness = transport_harness

    class AbortAfterReply(RecordingBufferedProtocol):
        def buffer_updated(self, nbytes):
            super().buffer_updated(nbytes)
            self.transport.write(b"reply")
            self.transport.abort()

    _, protocol, sock = harness.make_transport(AbortAfterReply())
    harness.drain()
    harness.receive(b"data")

    assert protocol.events == [("made",), ("get", 4), ("updated", b"data"), ("lost", None)]
    assert harness.proactor.writes[0].data == b"reply"
    assert harness.proactor.writes[0].future.cancelled()
    assert harness.proactor.reads[-1].future.cancelled()
    assert sock.close_count == 1
    assert not harness.errors


def test_eof_callback_error_is_reported_with_transport_and_protocol(transport_harness):
    harness = transport_harness
    error = RuntimeError("EOF callback failed")

    class BrokenEOF(RecordingBufferedProtocol):
        def eof_received(self):
            self.events.append(("eof",))
            raise error

    transport, protocol, sock = harness.make_transport(BrokenEOF())
    harness.drain()
    harness.receive(b"")

    assert protocol.events == [("made",), ("eof",), ("lost", error)]
    assert len(harness.errors) == 1
    assert harness.errors[0]["exception"] is error
    assert harness.errors[0]["transport"] is transport
    assert harness.errors[0]["protocol"] is protocol
    assert "eof_received()" in harness.errors[0]["message"]
    assert sock.close_count == 1
