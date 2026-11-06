"""Native transport request routing does not allocate asyncio Futures."""

from asyncio import base_events
from collections import deque
import errno
from types import SimpleNamespace
import weakref

import pytest
from test_phase3_transport_contracts import FakeSocket, RecordingProtocol

from uringloop.native_proactor import NativeIoUringProactor
from uringloop.native_stream import NativeStreamTransport


class Request:
    def __init__(self, kind, buffer=None, target=None, file=None):
        self.kind = kind
        self.buffer = memoryview(buffer) if buffer is not None else None
        self.target = target
        self.state = "prepared"
        self.local = True
        self.fd = file.fileno() if hasattr(file, "fileno") else None


class Ring:
    cq_ready = 0
    cq_overflowed = False

    def __init__(self):
        self.requests = []
        self.completions = deque()
        self.closed = False
        self.submit_error = None
        self.submitted_fd_operations = []

    def prepare_recv(self, file, buffer):
        request = Request("recv", buffer, file=file)
        self.requests.append(request)
        return request

    def prepare_send(self, file, buffer):
        request = Request("send", buffer, file=file)
        self.requests.append(request)
        return request

    def prepare_cancel(self, request):
        cancellation = Request("cancel", target=request)
        self.requests.append(cancellation)
        return cancellation

    def abandon_prepared(self, request):
        assert request.state == "prepared"
        if not request.local:
            raise ValueError("request is no longer in the local submission queue")
        request.kind = "nop"

    def submit(self):
        # Match liburing: local SQEs are published before enter can fail, while
        # a failed enter leaves the C Request logically "prepared".
        for request in self.requests:
            request.local = False
        if self.submit_error is not None:
            raise self.submit_error
        for request in self.requests:
            if request.state == "prepared":
                if request.kind in {"recv", "send"}:
                    self.submitted_fd_operations.append((request.kind, request.fd))
                request.state = "submitted"

    def wait(self, **kwargs):
        completions = list(self.completions)
        self.completions.clear()
        return completions

    def close(self):
        self.closed = True
        for request in self.requests:
            request.state = "released"


@pytest.fixture
def proactor(monkeypatch):
    monkeypatch.setattr("uringloop.native_proactor._uringcore.Ring", lambda *args, **kwargs: Ring())
    value = NativeIoUringProactor()
    monkeypatch.setattr(value, "_new_future", lambda: pytest.fail("transport operations must not create Futures"))
    try:
        yield value
    finally:
        # Individual tests explicitly retire callbacks; don't model a blocking
        # native wait in teardown. The real close path has its own test below.
        if value._ring is not None:
            value._ring.close()
            value._ring = None
            value._cache.clear()
            value._transport_requests.clear()


@pytest.mark.parametrize("operation", ["recv", "send"])
def test_transport_operation_routes_request_without_future(proactor, operation):
    completions = []
    file = object()
    payload = bytearray(b"payload")
    request = getattr(proactor, f"{operation}_transport")(file, payload, completions.append)
    pending = proactor._cache[request]
    assert pending.future is None
    assert pending.file is file
    assert request.buffer.obj is payload
    assert proactor._pending_submit
    completion = SimpleNamespace(request=request, result=3, final=True)
    proactor._handle_completion(completion)
    assert completions == [completion]
    assert request not in proactor._cache
    assert request not in proactor._transport_requests
    assert proactor._results == []


def test_callback_validated_before_prepare(proactor):
    with pytest.raises(TypeError, match="callable"):
        proactor.recv_transport(object(), bytearray(4), None)
    assert not proactor._ring.requests


def test_transport_callback_pins_owner_until_final_retirement(proactor):
    class Owner:
        def completion(self, completion):
            pass

    owner = Owner()
    reference = weakref.ref(owner)
    proactor.register_transport(owner)
    request = proactor.recv_transport(object(), bytearray(4), owner.completion)
    del owner
    assert reference() is not None
    proactor.cancel_transport(request)
    assert reference() is not None
    proactor._handle_completion(SimpleNamespace(request=request, result=-errno.ECANCELED, final=True))
    assert reference() is None


def test_cancel_abandons_prepared_request_without_submitting_and_retains_callback(proactor):
    seen = []
    payload = bytearray(b"owned")
    request = proactor.recv_transport(object(), payload, seen.append)
    proactor._ring.submit_error = OSError(errno.ENOMEM, "must not submit to cancel")
    proactor.cancel_transport(request)
    proactor.cancel_transport(request)
    assert request.state == "prepared"
    assert request.kind == "nop"
    assert request.buffer.obj is payload
    assert proactor._ring.requests == [request]
    assert request in proactor._cache
    assert request in proactor._transport_requests
    proactor._ring.submit_error = None
    proactor._flush()
    completion = SimpleNamespace(request=request, result=0, final=True)
    proactor._handle_completion(completion)
    assert seen == [completion]
    assert not proactor._cache
    assert not proactor._transport_requests


def test_cancel_submitted_request_retains_callback_until_original_retirement(proactor):
    request = proactor.recv_transport(object(), bytearray(4), lambda completion: None)
    proactor._flush()
    proactor.cancel_transport(request)
    assert request.state == "submitted"
    cancellation = proactor._ring.requests[-1]
    assert cancellation.kind == "cancel"
    assert cancellation.target is request
    assert request in proactor._cache
    assert cancellation in proactor._cache
    proactor._handle_completion(SimpleNamespace(request=cancellation, result=-errno.ENOENT, final=True))
    assert request in proactor._cache
    proactor._handle_completion(SimpleNamespace(request=request, result=0, final=True))
    assert not proactor._cache


def test_cancel_unknown_request_does_not_prepare_kernel_operation(proactor):
    proactor.cancel_transport(Request("recv"))
    assert not proactor._ring.requests


def test_proactor_close_aborts_native_transports_before_ring_close(proactor):
    calls = []

    class Owner:
        def abort(self):
            calls.append(("abort", proactor._ring.closed))

    owner = Owner()
    ring = proactor._ring
    proactor.register_transport(owner)
    proactor.close()
    assert calls == [("abort", False)]
    assert ring.closed
    assert proactor._ring is None


def test_proactor_close_prefers_loop_shutdown_hook(proactor):
    calls = []

    class Owner:
        def abort(self):
            pytest.fail("loop shutdown must force-detach queued server loss callbacks")

        def _close_for_loop(self):
            calls.append("shutdown")

    owner = Owner()
    proactor.register_transport(owner)
    proactor.close()
    assert calls == ["shutdown"]


def test_proactor_close_finishes_peers_before_raising_shutdown_error(proactor):
    calls = []
    error = RuntimeError("shutdown callback failed")

    class Owner:
        def __init__(self, fail):
            self.fail = fail

        def abort(self):
            calls.append(self.fail)
            if self.fail:
                raise error

    bad = Owner(True)
    good = Owner(False)
    ring = proactor._ring
    proactor.register_transport(bad)
    proactor.register_transport(good)
    with pytest.raises(RuntimeError) as raised:
        proactor.close()
    assert raised.value is error
    assert sorted(calls) == [False, True]
    assert ring.closed
    assert proactor._ring is None


def test_transport_apis_reject_closed_proactor(proactor):
    proactor.close()
    for method, args in [
        (proactor.recv_transport, (object(), bytearray(4), lambda completion: None)),
        (proactor.send_transport, (object(), b"data", lambda completion: None)),
        (proactor.cancel_transport, (Request("recv"),)),
    ]:
        with pytest.raises(RuntimeError, match="closed"):
            method(*args)


def test_transport_prepare_retries_full_sq_after_flushing_without_future(proactor, monkeypatch):
    first = proactor.send_transport(object(), b"first", lambda completion: None)
    original = proactor._ring.prepare_recv
    calls = []

    def prepare(file, buffer):
        calls.append(first.state)
        if len(calls) == 1:
            raise BufferError("SQ full")
        return original(file, buffer)

    monkeypatch.setattr(proactor._ring, "prepare_recv", prepare)
    second = proactor.recv_transport(object(), bytearray(4), lambda completion: None)
    assert calls == ["prepared", "submitted"]
    assert second in proactor._cache
    assert proactor._cache[second].future is None


def test_cancel_prepare_failure_keeps_request_and_exported_buffer_until_final(proactor, monkeypatch):
    seen = []
    payload = bytearray(b"owned")
    request = proactor.send_transport(object(), payload, seen.append)
    proactor._flush()
    error = OSError(errno.ENOMEM, "cancel prepare failed")

    def fail(*args):
        raise error

    monkeypatch.setattr(proactor._ring, "prepare_cancel", fail)
    with pytest.raises(OSError) as raised:
        proactor.cancel_transport(request)
    assert raised.value is error
    assert request.state == "submitted"
    assert request.buffer.obj is payload
    assert request in proactor._transport_requests
    assert proactor._cache[request].handler == seen.append
    completion = SimpleNamespace(request=request, result=5, final=True)
    proactor._handle_completion(completion)
    assert seen == [completion]
    assert not proactor._cache


def test_cancel_error_reports_request_without_retiring_original(proactor):
    errors = []
    proactor.set_loop(SimpleNamespace(call_exception_handler=errors.append))
    request = proactor.recv_transport(object(), bytearray(4), lambda completion: None)
    proactor._flush()
    proactor.cancel_transport(request)
    cancellation = proactor._ring.requests[-1]
    proactor._handle_completion(SimpleNamespace(request=cancellation, result=-errno.EINVAL, final=True))
    assert len(errors) == 1
    assert errors[0]["request"] is request
    assert errors[0]["exception"].errno == errno.EINVAL
    assert request in proactor._cache
    assert request in proactor._transport_requests
    proactor._handle_completion(SimpleNamespace(request=request, result=-errno.ECANCELED, final=True))
    assert not proactor._cache


def test_reentrant_final_callback_can_prepare_next_request_without_stale_delivery(proactor):
    seen = []
    next_requests = []

    def first_done(completion):
        seen.append(completion)
        next_requests.append(proactor.send_transport(object(), b"next", seen.append))

    first = proactor.send_transport(object(), b"first", first_done)
    completion = SimpleNamespace(request=first, result=5, final=True)
    proactor._handle_completion(completion)
    assert first not in proactor._cache
    assert next_requests[0] in proactor._cache
    proactor._handle_completion(completion)
    assert seen == [completion]
    final = SimpleNamespace(request=next_requests[0], result=4, final=True)
    proactor._handle_completion(final)
    assert seen == [completion, final]
    assert not proactor._cache


@pytest.mark.parametrize("failure_site", ["cache", "tracking"])
def test_registration_failure_abandons_fd_sqe_before_error_returns(proactor, failure_site):
    error = MemoryError("scripted registration allocation failure")

    class FailingCache(dict):
        def __setitem__(self, key, value):
            raise error

    class FailingTracking(set):
        def add(self, key):
            raise error

    if failure_site == "cache":
        proactor._cache = FailingCache()
    else:
        proactor._transport_requests = FailingTracking()
    with pytest.raises(MemoryError) as raised:
        proactor.send_transport(object(), b"owned", lambda completion: pytest.fail("abandoned callback"))
    assert raised.value is error
    [request] = proactor._ring.requests
    assert request.kind == "nop"
    assert request.state == "prepared"
    assert proactor._pending_submit
    assert not proactor._cache
    assert not proactor._transport_requests
    proactor._flush()
    proactor._handle_completion(SimpleNamespace(request=request, result=0, final=True))
    assert proactor._results == []


def test_registration_record_allocation_failure_happens_before_prepare(proactor, monkeypatch):
    error = MemoryError("scripted record allocation failure")

    def fail(*args):
        raise error

    monkeypatch.setattr("uringloop.native_proactor._Pending", fail)
    with pytest.raises(MemoryError) as raised:
        proactor.send_transport(object(), b"owned", lambda completion: None)
    assert raised.value is error
    assert not proactor._ring.requests
    assert not proactor._pending_submit


@pytest.mark.parametrize("operation", ["recv", "send", "both"])
@pytest.mark.parametrize("action", ["abort", "_close_for_loop"])
def test_c_transport_shutdown_cannot_submit_closed_fd_after_submit_failure(proactor, operation, action):
    loop = base_events.BaseEventLoop()
    loop._proactor = proactor
    proactor.set_loop(loop)
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    sock, protocol = FakeSocket(), RecordingProtocol()
    transport = NativeStreamTransport(loop, sock, protocol)

    def drain():
        for _ in range(30):
            if not loop._ready:
                return
            for _ in range(len(loop._ready)):
                loop._ready.popleft()._run()
        raise AssertionError("transport callbacks did not settle")

    try:
        if operation == "send":
            transport.pause_reading()
        drain()
        if operation != "recv":
            transport.write(b"do not send to a reused fd")
            drain()
        ring = proactor._ring
        original = list(ring.requests)
        assert [request.kind for request in original] == {"recv": ["recv"], "send": ["send"], "both": ["recv", "send"]}[
            operation
        ]
        ring.submit_error = OSError(errno.ENOMEM, "scripted submission failure")
        getattr(transport, action)()
        drain()
        assert sock.close_count == 1
        assert all(request.kind == "nop" for request in original)
        assert not ring.submitted_fd_operations
        # Even with a successful later submit and numeric fd reuse, only NOPs
        # may enter the kernel. The real C state and Python routing stay alive
        # until their final completions, not merely until connection_lost.
        ring.submit_error = None
        proactor._flush()
        assert not ring.submitted_fd_operations
        for request in original:
            proactor._handle_completion(SimpleNamespace(request=request, result=0, final=True))
        drain()
        assert not proactor._cache
        assert not proactor._transport_requests
        assert sum(event[0] == "lost" for event in protocol.events) == (action == "abort")
        assert not errors
    finally:
        transport.abort()
        drain()
        loop.close()


@pytest.mark.parametrize("operation", ["recv", "send", "both"])
@pytest.mark.parametrize("failure", ["oserror", "no_progress", "interrupt"])
def test_published_submission_failure_retires_ring_before_c_transport_closes_fd(proactor, operation, failure):
    loop = base_events.BaseEventLoop()
    loop._proactor = proactor
    proactor.set_loop(loop)
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    ring = proactor._ring

    class OrderedSocket(FakeSocket):
        def close(self):
            assert ring.closed, "published SQEs must retire before fd close"
            super().close()

    sock, protocol = OrderedSocket(), RecordingProtocol()
    transport = NativeStreamTransport(loop, sock, protocol)

    def drain():
        for _ in range(30):
            if not loop._ready:
                return
            for _ in range(len(loop._ready)):
                loop._ready.popleft()._run()
        raise AssertionError("transport callbacks did not settle")

    try:
        if operation == "send":
            transport.pause_reading()
        drain()
        if operation != "recv":
            transport.write(b"old socket payload")
            drain()
        original = list(ring.requests)
        assert all(request.local and request.state == "prepared" for request in original)
        error = {
            "oserror": OSError(errno.ENOMEM, "published submission failure"),
            "no_progress": RuntimeError("submission made no progress"),
            "interrupt": KeyboardInterrupt(),
        }[failure]
        ring.submit_error = error
        with pytest.raises(type(error)) as raised:
            proactor._flush()
        assert raised.value is error
        assert all(not request.local and request.state == "released" for request in original)
        assert proactor._ring is None
        assert not proactor._pending_submit
        assert not proactor._cache
        assert not proactor._transport_requests
        assert sock.close_count == 1
        assert not ring.submitted_fd_operations
        # The proactor cannot retry the old published fd operations.
        proactor._flush()
        assert not ring.submitted_fd_operations
        drain()
        # Fatal proactor shutdown uses the synchronous loop-close hook, not a
        # connection_lost callback that cannot be driven by a closed ring.
        assert protocol.events == [("made",)]
        assert not errors
    finally:
        transport.abort()
        drain()
        loop.close()


def test_submission_failure_settles_public_futures_and_finishes_all_transport_peers(proactor):
    loop = base_events.BaseEventLoop()
    error = OSError(errno.ENOMEM, "published submission failure")
    future = loop.create_future()
    request = proactor._ring.prepare_recv(object(), bytearray(4))
    proactor._register(request, future, object(), None)
    seen = []

    class Owner:
        def __init__(self, fails):
            self.fails = fails

        def abort(self):
            assert request.state == "released"
            seen.append(self.fails)
            if self.fails:
                raise RuntimeError("peer abort failed")

    good, bad = Owner(False), Owner(True)
    proactor.register_transport(good)
    proactor.register_transport(bad)
    proactor._ring.submit_error = error
    try:
        with pytest.raises(OSError) as raised:
            proactor._flush()
        assert raised.value is error
        assert sorted(seen) == [False, True]
        assert future.exception() is error
        assert proactor._ring is None
        assert not proactor._cache
        assert not proactor._transports
    finally:
        loop.close()


def test_failed_submission_detaches_server_without_draining_ready_callbacks(proactor):
    loop = base_events.BaseEventLoop()
    loop._proactor = proactor
    proactor.set_loop(loop)
    events = []
    server = object()
    loop._native_server_attach = lambda owner, transport: events.append(("attach", owner))
    loop._native_server_detach = lambda owner, transport: events.append(("detach", owner))
    sock, protocol = FakeSocket(), RecordingProtocol()
    transport = NativeStreamTransport(loop, sock, protocol, server=server)
    try:
        for _ in range(3):
            for _ in range(len(loop._ready)):
                loop._ready.popleft()._run()
        assert events == [("attach", server)]
        proactor._ring.submit_error = OSError(errno.ENOMEM, "published submission failure")
        with pytest.raises(OSError):
            proactor._flush()
        assert events == [("attach", server), ("detach", server)]
        assert sock.close_count == 1
        # No callback draining: a subsequent close must not depend on any lost
        # callback running before the loop discards its ready queue.
        proactor.close()
        loop.close()
        assert events == [("attach", server), ("detach", server)]
        assert protocol.events == [("made",)]
    finally:
        transport._close_for_loop()
        loop.close()
