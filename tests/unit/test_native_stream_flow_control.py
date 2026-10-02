"""Deterministic native flow-control gates; these do not replace kernel tests."""

import asyncio
import errno
import random
import socket

import pytest
from test_native_stream_transport import NativeHarness
from test_phase3_transport_contracts import RecordingProtocol, TransportHarness


@pytest.fixture
def native_harness():
    harness = NativeHarness()
    try:
        yield harness
    finally:
        harness.close()


def finish_writes(harness):
    emitted = bytearray()
    harness.drain()
    for _ in range(100):
        pending = [request for request in harness.proactor.writes if not request.completed and not request.cancelled]
        if not pending:
            return bytes(emitted)
        assert len(pending) == 1
        request = pending[0]
        emitted.extend(request.buffer)
        harness.complete(request, len(request.buffer))
    raise AssertionError("write producer failed to settle")


@pytest.mark.parametrize("high,low", [(0, 0), (4, 0), (4, 2), (4, 4)])
def test_watermark_equalities_and_zero_are_exact(native_harness, high, low):
    harness = native_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    transport.set_write_buffer_limits(high=high, low=low)
    transport.write(b"x" * high)
    assert protocol.events == [("made",)]
    transport.write(b"x")
    assert protocol.events == [("made",), ("pause",)]
    harness.drain()
    while transport.get_write_buffer_size() > low:
        request = next(item for item in harness.proactor.writes if not item.completed)
        count = min(len(request.buffer), transport.get_write_buffer_size() - low)
        harness.complete(request, count)
    assert transport.get_write_buffer_size() == low
    assert protocol.events == [("made",), ("pause",), ("resume",)]
    finish_writes(harness)
    assert sum(event[0] == "resume" for event in protocol.events) == 1


def test_limit_changes_recheck_buffer_immediately_and_invalid_changes_are_atomic(native_harness):
    harness = native_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    transport.write(b"1234")
    transport.set_write_buffer_limits(high=4, low=2)
    assert protocol.events == [("made",)]
    transport.set_write_buffer_limits(high=3, low=1)
    assert protocol.events[-1] == ("pause",)
    transport.set_write_buffer_limits(high=8, low=4)
    assert protocol.events[-1] == ("resume",)
    assert transport.get_write_buffer_size() == 4
    for kwargs in ({"high": -1}, {"low": -1}, {"high": 1, "low": 2}):
        with pytest.raises(ValueError):
            transport.set_write_buffer_limits(**kwargs)
        assert transport.get_write_buffer_limits() == (4, 8)
    transport.set_write_buffer_limits(low=3)
    assert transport.get_write_buffer_limits() == (3, 12)
    transport.set_write_buffer_limits(high=8)
    assert transport.get_write_buffer_limits() == (2, 8)


def test_pause_callback_can_change_limits_and_reentrantly_resume(native_harness):
    harness = native_harness

    class ChangeLimits(RecordingProtocol):
        def pause_writing(self):
            super().pause_writing()
            self.transport.set_write_buffer_limits(high=16, low=8)

        def resume_writing(self):
            super().resume_writing()
            self.transport.pause_reading()

    transport, protocol, _ = harness.make_transport(ChangeLimits())
    harness.drain()
    transport.set_write_buffer_limits(high=2, low=0)
    transport.write(b"first")
    assert protocol.events == [("made",), ("pause",), ("resume",)]
    assert not transport.is_reading()
    assert finish_writes(harness) == b"first"
    assert protocol.events == [("made",), ("pause",), ("resume",)]


@pytest.mark.parametrize("terminal", ["resume", "close", "abort"])
def test_streamwriter_drain_cancellation_does_not_poison_other_waiters(native_harness, terminal):
    harness = native_harness
    reader = asyncio.StreamReader(loop=harness.loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=harness.loop)
    transport, _, _ = harness.make_transport(protocol)
    harness.drain()
    writer = asyncio.StreamWriter(transport, protocol, reader, harness.loop)
    transport.set_write_buffer_limits(high=1, low=0)
    writer.write(b"pending")
    harness.drain()
    tasks = [harness.loop.create_task(writer.drain()) for _ in range(3)]
    harness.drain()
    assert all(not task.done() for task in tasks)
    tasks[0].cancel()
    harness.drain()
    assert tasks[0].cancelled()
    assert all(not task.done() for task in tasks[1:])
    if terminal == "resume":
        finish_writes(harness)
    else:
        getattr(transport, terminal)()
        finish_writes(harness)
    harness.drain()
    assert all(task.done() for task in tasks)
    assert [task.result() for task in tasks[1:]] == [None, None]
    writer.close()
    harness.drain()


@pytest.mark.parametrize("action", ["close", "abort"])
@pytest.mark.parametrize("callback", ["made", "data", "pause", "resume", "eof"])
def test_terminal_action_inside_each_protocol_callback_is_exactly_once(native_harness, action, callback):
    harness = native_harness

    class Terminates(RecordingProtocol):
        def act(self, name):
            if name == callback:
                if name in {"made", "data", "eof"}:
                    self.transport.write(b"reply")
                getattr(self.transport, action)()

        def connection_made(self, transport):
            super().connection_made(transport)
            self.act("made")

        def data_received(self, data):
            super().data_received(data)
            self.act("data")

        def pause_writing(self):
            super().pause_writing()
            self.act("pause")

        def resume_writing(self):
            super().resume_writing()
            self.act("resume")

        def eof_received(self):
            super().eof_received()
            self.act("eof")
            return True

    transport, protocol, sock = harness.make_transport(Terminates())
    harness.drain()
    if callback == "data":
        harness.receive(b"request")
    elif callback == "eof":
        harness.receive(b"")
    elif callback in {"pause", "resume"}:
        transport.set_write_buffer_limits(high=2, low=0)
        transport.write(b"reply")
        harness.drain()
        if callback == "resume":
            finish_writes(harness)
    output = finish_writes(harness)
    assert transport.is_closing()
    assert sock.close_count == 1
    assert sum(event[0] == "lost" for event in protocol.events) == 1
    assert protocol.events[-1] == ("lost", None)
    assert not harness.errors
    if action == "close" and callback != "resume":
        assert output == b"reply"
    elif action == "abort":
        assert output == b""
    transport.close()
    transport.abort()
    harness.drain()
    assert sock.close_count == 1


@pytest.mark.parametrize("operation", ["recv", "send"])
def test_prepare_failure_closes_and_keeps_original_exception(native_harness, monkeypatch, operation):
    harness = native_harness
    error = OSError(errno.ENOMEM, "scripted prepare failure")

    def fail(*args):
        raise error

    if operation == "recv":
        monkeypatch.setattr(harness.proactor, "recv_transport", fail)
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    if operation == "send":
        monkeypatch.setattr(harness.proactor, "send_transport", fail)
        transport.write(b"pending")
        harness.drain()
    assert protocol.events[-1] == ("lost", error)
    assert transport.get_write_buffer_size() == 0
    assert sock.close_count == 1
    assert harness.errors[-1]["exception"] is error
    assert harness.errors[-1]["transport"] is transport


def test_cancel_failure_attempts_both_requests_and_still_closes(native_harness, monkeypatch):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"pending")
    harness.drain()
    attempted = []
    error = RuntimeError("scripted cancellation failure")

    def fail(request):
        attempted.append(request)
        raise error

    monkeypatch.setattr(harness.proactor, "cancel_transport", fail)
    transport.abort()
    harness.drain()
    assert attempted == [harness.proactor.reads[0], harness.proactor.writes[0]]
    assert [context["exception"] for context in harness.errors] == [error, error]
    assert protocol.events[-1] == ("lost", None)
    assert sock.close_count == 1
    assert transport.get_write_buffer_size() == 0


def test_half_close_shutdown_failure_is_terminal_after_pending_write(native_harness, monkeypatch):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    calls = []
    error = OSError(errno.EPIPE, "scripted shutdown failure")

    def fail(how):
        calls.append(how)
        raise error

    monkeypatch.setattr(sock, "shutdown", fail)
    transport.write(b"tail")
    transport.write_eof()
    assert calls == []
    assert finish_writes(harness) == b"tail"
    assert calls == [socket.SHUT_WR]
    assert protocol.events[-1] == ("lost", error)
    assert harness.errors[-1]["exception"] is error
    assert sock.close_count == 1


@pytest.mark.parametrize("handler_action", ["close", "raise"])
def test_exception_handler_reentry_cannot_duplicate_terminal_cleanup(native_harness, handler_action):
    harness = native_harness
    error = RuntimeError("scripted protocol failure")
    handler_error = RuntimeError("scripted exception handler failure")
    reports = []

    class Broken(RecordingProtocol):
        def data_received(self, data):
            raise error

    transport, protocol, sock = harness.make_transport(Broken())
    harness.drain()

    def handler(loop, context):
        reports.append(context)
        transport.close()
        transport.abort()
        if handler_action == "raise":
            raise handler_error

    harness.loop.set_exception_handler(handler)
    default_reports = []
    harness.loop.default_exception_handler = default_reports.append
    harness.receive(b"bad")
    assert reports[0]["exception"] is error
    assert reports[0]["transport"] is transport
    assert protocol.events[-1] == ("lost", error)
    assert sock.close_count == 1
    assert len(default_reports) == (handler_action == "raise")
    if default_reports:
        assert default_reports[0]["exception"] is handler_error


@pytest.mark.parametrize("terminal", ["resume", "close", "abort"])
def test_paused_full_read_and_eof_are_bounded_and_delivered_at_most_once(native_harness, terminal):
    harness = native_harness

    class PausesAfterData(RecordingProtocol):
        def data_received(self, data):
            super().data_received(data)
            self.transport.pause_reading()

    transport, protocol, sock = harness.make_transport(PausesAfterData())
    protocol.keep_open = True
    harness.drain()
    transport.pause_reading()
    payload = b"x" * (64 * 1024)
    harness.receive(payload)
    assert len(harness.proactor.reads) == 1
    assert protocol.events == [("made",)]
    transport.resume_reading()
    transport.resume_reading()
    harness.drain()
    assert protocol.events == [("made",), ("data", payload)]
    assert len(harness.proactor.reads) == 1  # data callback paused before rearm
    transport.resume_reading()
    harness.drain()
    assert len(harness.proactor.reads) == 2
    transport.pause_reading()
    harness.receive(b"")
    assert protocol.events[-1][0] == "data"
    if terminal == "resume":
        transport.resume_reading()
        transport.resume_reading()
        harness.drain()
        assert protocol.events[-1] == ("eof",)
        assert sum(event[0] == "eof" for event in protocol.events) == 1
        assert not sock.closed
        transport.write(b"after read EOF")
        assert finish_writes(harness) == b"after read EOF"
        transport.close()
    else:
        getattr(transport, terminal)()
    harness.drain()
    assert len(harness.proactor.reads) == 2
    assert sum(event[0] == "data" for event in protocol.events) == 1
    assert sum(event[0] == "lost" for event in protocol.events) == 1
    assert sock.close_count == 1
    if terminal != "resume":
        assert not any(event[0] == "eof" for event in protocol.events)


@pytest.mark.parametrize("broken_callback", ["pause", "resume"])
def test_flow_callback_failure_reports_without_losing_bytes_or_corrupting_transitions(native_harness, broken_callback):
    harness = native_harness
    error = RuntimeError("scripted flow callback failure")

    class BrokenFlow(RecordingProtocol):
        def pause_writing(self):
            super().pause_writing()
            if broken_callback == "pause":
                raise error

        def resume_writing(self):
            super().resume_writing()
            if broken_callback == "resume":
                raise error

    transport, protocol, sock = harness.make_transport(BrokenFlow())
    harness.drain()
    transport.set_write_buffer_limits(high=1, low=0)
    transport.write(b"first")
    transport.write(b"second")
    assert finish_writes(harness) == b"firstsecond"
    assert not transport.is_closing()
    assert not sock.closed
    assert protocol.events == [("made",), ("pause",), ("resume",)]
    assert len(harness.errors) == 1
    assert harness.errors[0]["exception"] is error
    assert harness.errors[0]["transport"] is transport
    assert harness.errors[0]["protocol"] is protocol
    transport.write(b"third")
    assert finish_writes(harness) == b"third"
    assert protocol.events[-2:] == [("pause",), ("resume",)]


def test_connection_made_failure_settles_waiter_with_original_error(native_harness):
    harness = native_harness
    error = RuntimeError("scripted connection_made failure")

    class BrokenMade(RecordingProtocol):
        def connection_made(self, transport):
            super().connection_made(transport)
            raise error

    waiter = harness.loop.create_future()
    transport, protocol, sock = harness.make_transport(BrokenMade(), waiter)
    harness.drain()
    assert waiter.done()
    assert waiter.exception() is error
    assert protocol.events == [("made",), ("lost", error)]
    assert harness.errors[-1]["exception"] is error
    assert harness.errors[-1]["transport"] is transport
    assert not harness.proactor.reads
    assert sock.close_count == 1


def test_eof_truth_conversion_failure_closes_without_rearm(native_harness):
    harness = native_harness
    error = RuntimeError("scripted EOF truth conversion failure")

    class BrokenTruth:
        def __bool__(self):
            raise error

    class BrokenEOF(RecordingProtocol):
        def eof_received(self):
            super().eof_received()
            return BrokenTruth()

    transport, protocol, sock = harness.make_transport(BrokenEOF())
    harness.drain()
    harness.receive(b"")
    assert protocol.events == [("made",), ("eof",), ("lost", error)]
    assert harness.errors[-1]["exception"] is error
    assert transport.is_closing()
    assert len(harness.proactor.reads) == 1
    assert sock.close_count == 1


@pytest.mark.parametrize("exception_type", [SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("callback", ["pause", "resume"])
def test_propagated_flow_exception_keeps_deferred_write_producer(native_harness, exception_type, callback):
    harness = native_harness
    error = exception_type("scripted flow interruption")

    class Interrupts(RecordingProtocol):
        def pause_writing(self):
            super().pause_writing()
            if callback == "pause":
                raise error

        def resume_writing(self):
            super().resume_writing()
            if callback == "resume":
                raise error

    transport, _, sock = harness.make_transport(Interrupts())
    harness.drain()
    transport.set_write_buffer_limits(high=4, low=2)
    if callback == "pause":
        with pytest.raises(exception_type) as raised:
            transport.write(b"first")
        assert raised.value is error
        harness.drain()
        assert len(harness.proactor.writes) == 1
        assert finish_writes(harness) == b"first"
    else:
        transport.write(b"first")
        transport.write(b"xy")
        harness.drain()
        with pytest.raises(exception_type) as raised:
            harness.complete_write(0)
        assert raised.value is error
        harness.drain()
        assert finish_writes(harness) == b"xy"
    assert transport.get_write_buffer_size() == 0
    assert not sock.closed


@pytest.mark.parametrize("operation", ["write", "write-rearm", "read-rearm"])
def test_schedule_failure_is_terminal_not_an_idle_pending_operation(native_harness, monkeypatch, operation):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    if operation == "write-rearm":
        transport.write(b"pending")
        harness.drain()
    original_call_soon = harness.loop.call_soon
    error = RuntimeError("scripted call_soon failure")
    failures = []

    def fail_once(callback, *args, context=None):
        if not failures:
            failures.append(callback)
            raise error
        return original_call_soon(callback, *args, context=context)

    monkeypatch.setattr(harness.loop, "call_soon", fail_once)
    propagated = None
    try:
        if operation == "write":
            transport.write(b"pending")
        elif operation == "write-rearm":
            harness.complete_write(0, 1)
        else:
            harness.receive(b"incoming")
    except RuntimeError as exc:
        propagated = exc
    harness.drain()
    assert transport.is_closing()
    assert sock.close_count == 1
    assert transport.get_write_buffer_size() == 0
    assert sum(event[0] == "lost" for event in protocol.events) == 1
    assert propagated is error or any(context.get("exception") is error for context in harness.errors)


@pytest.mark.parametrize("operation", ["write", "write-eof", "resume-read"])
def test_persistent_schedule_failure_closes_fd_and_detaches_server_without_callbacks(native_harness, monkeypatch, operation):
    harness = native_harness
    counts = {"attach": 0, "detach": 0}

    class Server:
        def _attach(self, *args):
            counts["attach"] += 1

        def _detach(self, *args):
            counts["detach"] += 1

    transport, protocol, sock = harness.make_transport(server=Server())
    harness.drain()
    error = RuntimeError("scripted persistent scheduler failure")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(harness.loop, "call_soon", fail)
    with pytest.raises(RuntimeError) as raised:
        if operation == "write":
            transport.write(b"pending")
        elif operation == "write-eof":
            transport.write_eof()
        else:
            transport.pause_reading()
            transport.resume_reading()
    assert raised.value is error
    assert transport.is_closing()
    assert not transport.is_reading()
    assert transport.get_write_buffer_size() == 0
    assert sock.close_count == 1
    assert counts == {"attach": 1, "detach": 1}
    assert protocol.events == [("made",)]  # an unavailable scheduler cannot run lost
    transport.abort()
    assert sock.close_count == 1


@pytest.mark.parametrize("exception_type", [RuntimeError, SystemExit, KeyboardInterrupt])
def test_direct_exception_report_failure_preserves_write_producer(native_harness, monkeypatch, exception_type):
    harness = native_harness
    callback_error = RuntimeError("scripted pause callback failure")
    handler_error = exception_type("scripted report failure")

    class BrokenPause(RecordingProtocol):
        def pause_writing(self):
            super().pause_writing()
            raise callback_error

    transport, _, sock = harness.make_transport(BrokenPause())
    harness.drain()
    transport.set_write_buffer_limits(high=2, low=0)
    reports = []

    def report(context):
        reports.append(context)
        raise handler_error

    monkeypatch.setattr(harness.loop, "call_exception_handler", report)
    with pytest.raises(exception_type) as raised:
        transport.write(b"pending")
    assert raised.value is handler_error
    assert reports[0]["exception"] is callback_error
    assert reports[0]["transport"] is transport
    assert finish_writes(harness) == b"pending"
    assert transport.get_write_buffer_size() == 0
    assert not sock.closed


@pytest.mark.parametrize("operation", ["read", "write"])
def test_reentrant_completion_getter_cannot_retire_the_next_request(native_harness, operation):
    harness = native_harness
    transport, protocol, _ = harness.make_transport()
    harness.drain()
    if operation == "write":
        transport.write(b"first")
        transport.write(b"second")
        harness.drain()
        request = harness.proactor.writes[0]
        count = 5
    else:
        request = harness.proactor.reads[0]
        request.buffer[:3] = b"old"
        count = 3
    callback = request.callback

    class ReentrantCompletion:
        @property
        def request(self):
            return request

        @property
        def result(self):
            harness.complete(request, count)
            return count

    callback(ReentrantCompletion())
    harness.drain()
    assert not transport.is_closing()
    assert not harness.errors
    if operation == "write":
        assert transport.get_write_buffer_size() == 6
        assert len(harness.proactor.writes) == 2
        assert harness.proactor.writes[1].buffer == b"second"
        assert finish_writes(harness) == b"second"
    else:
        assert protocol.events == [("made",), ("data", b"old")]
        assert len(harness.proactor.reads) == 2
        harness.receive(b"new")
        assert protocol.events[-1] == ("data", b"new")


def test_close_read_cancel_handler_interrupt_keeps_pending_write_progress(native_harness, monkeypatch):
    harness = native_harness
    transport, protocol, sock = harness.make_transport()
    harness.drain()
    transport.write(b"pending")
    # Keep the deferred producer queued: close must not depend on a caller
    # manually writing again after cancellation's exception handler interrupts.
    cancellation_error = RuntimeError("scripted read cancellation failure")
    handler_error = KeyboardInterrupt("scripted cancellation report interruption")
    read = harness.proactor.reads[0]
    original_cancel = harness.proactor.cancel_transport

    def cancel(request):
        if request is read:
            raise cancellation_error
        original_cancel(request)

    def handler(loop, context):
        assert context["exception"] is cancellation_error
        raise handler_error

    monkeypatch.setattr(harness.proactor, "cancel_transport", cancel)
    harness.loop.set_exception_handler(handler)
    with pytest.raises(KeyboardInterrupt) as raised:
        transport.close()
    assert raised.value is handler_error
    assert finish_writes(harness) == b"pending"
    assert protocol.events[-1] == ("lost", None)
    assert sock.close_count == 1


@pytest.mark.parametrize("seed", range(6))
def test_seeded_short_write_sequences_match_python_oracle(seed):
    rng = random.Random(seed)
    native = NativeHarness()
    oracle = TransportHarness()
    try:
        transports = [harness.make_transport()[0] for harness in (native, oracle)]
        for harness in (native, oracle):
            harness.drain()
        for transport in transports:
            transport.set_write_buffer_limits(high=12, low=4)
        chunks = [bytes([index]) * rng.randint(1, 15) for index in range(1, 9)]
        for chunk in chunks:
            for transport in transports:
                transport.write(chunk)
        native.drain()
        expected = b"".join(chunks)
        outputs = []
        for harness, transport in zip((native, oracle), transports):
            emitted = bytearray()
            cursor = 0
            while transport.get_write_buffer_size():
                operation = harness.proactor.writes[cursor]
                view = operation.buffer if harness is native else operation.data
                count = rng.randint(1, len(view))
                emitted.extend(view[:count])
                if harness is native:
                    harness.complete(operation, count)
                else:
                    operation.future.set_result(count)
                    harness.drain()
                cursor += 1
                assert transport.get_write_buffer_size() == len(expected) - len(emitted)
            outputs.append(bytes(emitted))
            transport.close()
            harness.drain()
        assert outputs == [expected, expected]
        protocols = [transport.get_protocol() for transport in transports]
        assert protocols[0].events == protocols[1].events == [("made",), ("pause",), ("resume",), ("lost", None)]
        assert not native.errors
        assert not oracle.errors
    finally:
        native.close()
        oracle.close()
