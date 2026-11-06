"""Failed native construction must never publish a usable partial state."""

import asyncio
from asyncio import base_events
import contextvars
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from test_phase3_transport_contracts import FakeSocket, RecordingProtocol

from uringloop import _uringcore


@pytest.mark.parametrize("failure", ["protocol", "extra", "proactor", "context", "callback", "registration", "schedule"])
def test_failed_initialization_rejects_operations_and_preserves_cleanup(failure, monkeypatch):
    loop = base_events.BaseEventLoop()
    sock, protocol = FakeSocket(), RecordingProtocol()
    registered, attached, detached = [], [], []
    loop._proactor = SimpleNamespace(register_transport=registered.append)
    loop._native_server_attach = lambda server, transport: attached.append(transport)
    loop._native_server_detach = lambda server, transport: detached.append(transport)
    error = MemoryError(f"injected {failure} failure")

    def fail(*args, **kwargs):
        raise error

    class BrokenCallbackLookup(_uringcore.StreamTransport):
        def __getattribute__(self, name):
            if name == "_write_pump":
                raise error
            return super().__getattribute__(name)

    state_type = BrokenCallbackLookup if failure == "callback" else _uringcore.StreamTransport
    state = state_type.__new__(state_type)
    try:
        with monkeypatch.context() as patch:
            if failure == "proactor":
                del loop._proactor
            elif failure == "context":
                patch.setattr(contextvars, "copy_context", fail)
            elif failure == "registration":
                patch.setattr(loop._proactor, "register_transport", fail)
            elif failure == "schedule":
                patch.setattr(loop, "call_soon", fail)
            expected = (
                AttributeError if failure in {"protocol", "proactor"}
                else TypeError if failure == "extra" else MemoryError
            )
            with pytest.raises(expected) as caught:
                state.__init__(
                    loop, sock, object() if failure == "protocol" else protocol,
                    extra=[] if failure == "extra" else {"token": "value"}, server=object(),
                )
            if expected is MemoryError:
                assert caught.value is error

        # Invoke base descriptors so the deliberately broken attribute lookup
        # cannot obscure the native initialization checks being tested.
        for method, args in (
            ("get_extra_info", ("socket",)), ("get_protocol", ()),
            ("set_protocol", (asyncio.Protocol(),)), ("write", (b"payload",)),
            ("writelines", ([b"payload"],)), ("set_write_buffer_limits", (1,)),
            ("write_eof", ()), ("pause_reading", ()), ("resume_reading", ()),
            ("close", ()), ("abort", ()), ("_connected", ()),
            ("_read_pump", ()), ("_write_pump", ()),
        ):
            with pytest.raises(RuntimeError, match="not initialized"):
                getattr(_uringcore.StreamTransport, method)(state, *args)
        with pytest.raises(RuntimeError, match="already initialized"):
            state.__init__(loop, sock, protocol)
        assert state._lost() is None
        assert not state.is_reading()
        assert not protocol.events
        assert attached == detached
        assert sock.close_count == (0 if failure == "protocol" else 1)
        # A successful registration followed by failed scheduling can retain
        # this unusable object; the proactor shutdown hook stays idempotent.
        if registered:
            assert registered == [state]
            assert state._close_for_loop() is None
            assert sock.close_count == 1
    finally:
        if not sock.closed:
            sock.close()
        loop.close()


def test_invalid_metadata_initialization_crash_is_guarded_in_child():
    script = textwrap.dedent("""
        import asyncio
        from asyncio import base_events
        import resource
        from types import SimpleNamespace
        from uringloop import _uringcore

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        loop = base_events.BaseEventLoop()
        state = _uringcore.StreamTransport.__new__(_uringcore.StreamTransport)
        try:
            state.__init__(loop, SimpleNamespace(close=lambda: None), asyncio.Protocol(), extra=[])
        except TypeError:
            pass
        else:
            raise AssertionError('invalid metadata must fail construction')
        try:
            state.get_extra_info('socket')
        except RuntimeError:
            pass
        else:
            raise AssertionError('failed construction must leave methods guarded')
        loop.close()
    """)
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_successfully_initialized_closed_transport_keeps_valid_metadata():
    loop = base_events.BaseEventLoop()
    loop._proactor = SimpleNamespace(register_transport=lambda transport: None)
    sock = FakeSocket()
    state = _uringcore.StreamTransport(loop, sock, RecordingProtocol(), extra={"token": "value"})
    try:
        assert state.get_extra_info("token") == "value"
        state.abort()
        assert state.is_closing()
        assert state.get_extra_info("token") == "value"
        assert sock.close_count == 1
    finally:
        state._close_for_loop()
        loop.close()
