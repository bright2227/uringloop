import asyncio
import gc
import weakref

import pytest

from uringloop import _uringcore


METHODS = (
    "connection_made",
    "data_received",
    "eof_received",
    "connection_lost",
    "pause_writing",
    "resume_writing",
)


class RecordingProtocol(asyncio.Protocol):
    def __init__(self):
        self.calls = []
        self.result = object()

    def connection_made(self, transport):
        self.calls.append(("connection_made", transport))
        return self.result

    def data_received(self, data):
        self.calls.append(("data_received", data))
        return self.result

    def eof_received(self):
        self.calls.append(("eof_received",))
        return self.result

    def connection_lost(self, exception):
        self.calls.append(("connection_lost", exception))
        return self.result

    def pause_writing(self):
        self.calls.append(("pause_writing",))
        return self.result

    def resume_writing(self):
        self.calls.append(("resume_writing",))
        return self.result


def test_protocol_dispatch_abi():
    assert _uringcore.ABI_VERSION == 13


def test_all_callbacks_forward_identity_and_result():
    protocol = RecordingProtocol()
    dispatch = _uringcore.ProtocolDispatcher(protocol=protocol)
    transport = object()
    data = b"payload"
    error = RuntimeError("lost")

    assert dispatch.get_protocol() is protocol
    assert dispatch.connection_made(transport) is protocol.result
    assert dispatch.data_received(data) is protocol.result
    assert dispatch.eof_received() is protocol.result
    assert dispatch.connection_lost(error) is protocol.result
    assert dispatch.pause_writing() is protocol.result
    assert dispatch.resume_writing() is protocol.result
    assert protocol.calls == [
        ("connection_made", transport),
        ("data_received", data),
        ("eof_received",),
        ("connection_lost", error),
        ("pause_writing",),
        ("resume_writing",),
    ]


def test_bound_methods_are_cached_until_explicit_refresh():
    protocol = RecordingProtocol()
    dispatch = _uringcore.ProtocolDispatcher(protocol)
    replacements = []
    protocol.data_received = replacements.append

    dispatch.data_received(b"cached")
    assert protocol.calls == [("data_received", b"cached")]
    assert replacements == []
    dispatch.set_protocol(protocol)
    dispatch.data_received(b"refreshed")
    assert replacements == [b"refreshed"]


@pytest.mark.parametrize("method", METHODS)
def test_callback_exceptions_preserve_identity(method):
    protocol = RecordingProtocol()
    error = RuntimeError(method)

    def fail(*args):
        raise error

    setattr(protocol, method, fail)
    dispatch = _uringcore.ProtocolDispatcher(protocol)
    args = (None,) if method in {"connection_made", "data_received", "connection_lost"} else ()

    with pytest.raises(RuntimeError) as raised:
        getattr(dispatch, method)(*args)
    assert raised.value is error
    assert dispatch.get_protocol() is protocol


def test_failed_method_lookup_preserves_complete_old_cache():
    old = RecordingProtocol()
    dispatch = _uringcore.ProtocolDispatcher(old)
    error = ValueError("lookup")

    class Broken(RecordingProtocol):
        @property
        def resume_writing(self):
            raise error

    with pytest.raises(ValueError) as raised:
        dispatch.set_protocol(Broken())
    assert raised.value is error
    assert dispatch.get_protocol() is old
    dispatch.data_received(b"old")
    dispatch.resume_writing()
    assert old.calls == [("data_received", b"old"), ("resume_writing",)]


def test_noncallable_method_replacement_preserves_old_protocol():
    old = RecordingProtocol()
    dispatch = _uringcore.ProtocolDispatcher(old)
    replacement = RecordingProtocol()
    replacement.pause_writing = None

    with pytest.raises(TypeError, match="pause_writing must be callable"):
        dispatch.set_protocol(replacement)
    assert dispatch.get_protocol() is old


def test_missing_method_replacement_preserves_old_protocol():
    old = RecordingProtocol()
    dispatch = _uringcore.ProtocolDispatcher(old)
    with pytest.raises(AttributeError, match="connection_made"):
        dispatch.set_protocol(object())
    assert dispatch.get_protocol() is old


def test_buffered_protocol_is_explicitly_rejected():
    old = RecordingProtocol()
    dispatch = _uringcore.ProtocolDispatcher(old)
    with pytest.raises(TypeError, match="separate receive dispatcher"):
        dispatch.set_protocol(asyncio.BufferedProtocol())
    assert dispatch.get_protocol() is old


def test_buffered_interface_lookup_exception_is_preserved():
    error = ValueError("get_buffer lookup")

    class Broken(RecordingProtocol):
        @property
        def get_buffer(self):
            raise error

    with pytest.raises(ValueError) as raised:
        _uringcore.ProtocolDispatcher(Broken())
    assert raised.value is error


def test_repeated_init_uses_atomic_replacement_semantics():
    old = RecordingProtocol()
    replacement = RecordingProtocol()
    dispatch = _uringcore.ProtocolDispatcher(old)
    dispatch.__init__(replacement)
    assert dispatch.get_protocol() is replacement
    dispatch.data_received(b"new")
    assert replacement.calls == [("data_received", b"new")]

    with pytest.raises(AttributeError):
        dispatch.__init__(object())
    assert dispatch.get_protocol() is replacement


def test_reentrant_replacement_during_lookup_is_rejected_atomically():
    old = RecordingProtocol()
    dispatch = _uringcore.ProtocolDispatcher(old)

    class Reentrant(RecordingProtocol):
        @property
        def data_received(self):
            dispatch.set_protocol(RecordingProtocol())

    with pytest.raises(RuntimeError, match="during method lookup"):
        dispatch.set_protocol(Reentrant())
    assert dispatch.get_protocol() is old
    dispatch.data_received(b"still old")
    assert old.calls == [("data_received", b"still old")]


def test_callback_can_replace_protocol_and_continue_using_old_receiver():
    replacement = RecordingProtocol()
    calls = []

    class Reentrant(RecordingProtocol):
        def data_received(self, data):
            dispatch.set_protocol(replacement)
            calls.append((self, data))
            dispatch.data_received(b"nested new callback")
            return self.result

    old = Reentrant()
    dispatch = _uringcore.ProtocolDispatcher(old)
    result = old.result
    reference = weakref.ref(old)
    del old

    assert dispatch.data_received(b"outer") is result
    assert calls[0][0] is reference()
    assert calls[0][1] == b"outer"
    assert replacement.calls == [("data_received", b"nested new callback")]
    calls.clear()
    assert reference() is None


def test_gc_collects_dispatcher_protocol_cycles_and_cached_methods():
    protocol = RecordingProtocol()
    dispatch = _uringcore.ProtocolDispatcher(protocol)
    protocol.dispatch = dispatch
    reference = weakref.ref(protocol)
    assert gc.is_tracked(dispatch)
    del protocol, dispatch
    gc.collect()
    assert reference() is None


def test_replacement_releases_old_protocol_and_cached_methods():
    protocol = RecordingProtocol()
    reference = weakref.ref(protocol)
    dispatch = _uringcore.ProtocolDispatcher(protocol)
    del protocol
    assert reference() is not None
    dispatch.set_protocol(RecordingProtocol())
    assert reference() is None


def test_uninitialized_dispatcher_fails_safely_and_can_be_initialized():
    dispatch = _uringcore.ProtocolDispatcher.__new__(_uringcore.ProtocolDispatcher)
    with pytest.raises(RuntimeError, match="not initialized"):
        dispatch.get_protocol()
    with pytest.raises(RuntimeError, match="not initialized"):
        dispatch.eof_received()
    protocol = RecordingProtocol()
    dispatch.__init__(protocol)
    assert dispatch.get_protocol() is protocol


def test_lookup_finalizer_can_observe_only_complete_replacement():
    calls = []
    replacement = RecordingProtocol()

    class Old(RecordingProtocol):
        def __del__(self):
            calls.append(dispatch.get_protocol())
            dispatch.data_received(b"finalizer")

    old = Old()
    dispatch = _uringcore.ProtocolDispatcher(old)
    del old
    dispatch.set_protocol(replacement)
    assert calls == [replacement]
    assert replacement.calls == [("data_received", b"finalizer")]
