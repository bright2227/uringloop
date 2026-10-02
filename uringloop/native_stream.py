"""Experimental one-shot native plaintext stream transport selection.

The wrapper supplies asyncio identity, socket metadata and compatibility flags.
Its hot public methods are bound C state methods, not Python forwarding calls.
State transitions, read/write completion and protocol calls live in C.
BufferedProtocol (including sslproto) remains on the counted Python transport
until its separate native receive path is implemented.
"""

from asyncio import base_events, constants, transports, trsock

from uringloop import _uringcore


class NativeStreamTransport(transports._FlowControlMixin):
    # Initial SSL connections use the Python BufferedProtocol path. An upgrade
    # of an existing native plaintext transport would replace its protocol with
    # sslproto (BufferedProtocol), which this milestone cannot yet receive into.
    _start_tls_compatible = False
    _sendfile_compatible = constants._SendfileMode.FALLBACK

    @property
    def _protocol_paused(self):
        # CPython's sendfile fallback requires the mixin identity and this flag.
        # The native state, not duplicate Python mixin fields, owns watermarks.
        return self._state._protocol_paused

    def __init__(self, loop, sock, protocol, waiter=None, extra=None, server=None):
        if hasattr(self, "_state"):
            raise RuntimeError("stream transport is already initialized")
        metadata = dict(extra or {})
        metadata["socket"] = trsock.TransportSocket(sock)
        for key, getter in (("sockname", sock.getsockname), ("peername", sock.getpeername)):
            try:
                metadata[key] = getter()
            except OSError:
                pass
        base_events._set_nodelay(sock)
        # Do not initialize a second Python flow-control state. The mixin is
        # retained only for asyncio's nominal transport/sendfile compatibility.
        transports.BaseTransport.__init__(self, metadata)
        self._loop = loop
        state = _uringcore.StreamTransport.__new__(_uringcore.StreamTransport)
        self._state = state
        for name in (
            "write",
            "writelines",
            "close",
            "abort",
            "can_write_eof",
            "write_eof",
            "set_protocol",
            "get_protocol",
            "get_extra_info",
            "get_write_buffer_size",
            "set_write_buffer_limits",
            "get_write_buffer_limits",
            "is_closing",
            "is_reading",
            "pause_reading",
            "resume_reading",
            "_close_for_loop",
        ):
            setattr(self, name, getattr(state, name))
        state.__init__(loop, sock, protocol, waiter, metadata, server, owner=self)
