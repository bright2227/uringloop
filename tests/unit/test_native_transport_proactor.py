"""Native transport request routing does not allocate asyncio Futures."""

from collections import deque
import errno
from types import SimpleNamespace
import weakref

import pytest

from uringloop.native_proactor import NativeIoUringProactor


class Request:
    def __init__(self, kind, buffer=None, target=None):
        self.kind = kind
        self.buffer = memoryview(buffer) if buffer is not None else None
        self.target = target
        self.state = "prepared"


class Ring:
    cq_ready = 0
    cq_overflowed = False

    def __init__(self):
        self.requests = []
        self.completions = deque()
        self.closed = False

    def prepare_recv(self, file, buffer):
        request = Request("recv", buffer)
        self.requests.append(request)
        return request

    def prepare_send(self, file, buffer):
        request = Request("send", buffer)
        self.requests.append(request)
        return request

    def prepare_cancel(self, request):
        cancellation = Request("cancel", target=request)
        self.requests.append(cancellation)
        return cancellation

    def submit(self):
        for request in self.requests:
            if request.state == "prepared":
                request.state = "submitted"

    def wait(self, **kwargs):
        completions = list(self.completions)
        self.completions.clear()
        return completions

    def close(self):
        self.closed = True


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


def test_cancel_flushes_prepared_request_and_retains_original_callback(proactor):
    request = proactor.recv_transport(object(), bytearray(4), lambda completion: None)
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
