"""Stream transports for proactors whose sends return byte counts.

CPython's Windows proactor reports a completed *logical* write.  io_uring
reports a single syscall's byte count instead, so its transport must retain
and retry the remainder.  Read/lifecycle behavior is deliberately inherited
from asyncio for this correctness step; this is not the native Phase 3 fast
path.  See docs/phase3-stream-write-correctness.md.
"""

from asyncio import exceptions, proactor_events
import errno
import socket
import sys


class _PartialWriteMixin:
    """Keep the in-flight payload alive until all its bytes are acknowledged."""

    def __init__(self, *args, **kwargs):
        self._write_data = None
        super().__init__(*args, **kwargs)

    def _force_close(self, exc):
        # The proactor/request retains kernel-facing buffers until retirement.
        # The transport does not need an additional reference after abort.
        self._write_data = None
        super()._force_close(exc)

    def _loop_writing(self, f=None, data=None):
        if f is not None and f is not self._write_fut:
            # A callback already queued before abort must not restart writes.
            return

        try:
            if f is not None:
                payload = self._write_data
                sent = f.result()
                if type(sent) is not int or not 0 <= sent <= len(payload):
                    raise OSError(errno.EIO, "invalid stream write completion")
                if sent == 0:
                    raise ConnectionResetError("stream write made no progress")
                data = payload[sent:]
                self._write_fut = None
                self._write_data = None
                self._pending_write = 0

            # Finish an in-flight prefix before taking any later writes.
            if not data:
                data = self._buffer
                self._buffer = None

            if data:
                # write() already snapshots caller data.  Snapshot queued
                # bytearrays too, so the submitted payload cannot be mutated.
                payload = data if isinstance(data, memoryview) else memoryview(bytes(data))
                self._write_data = payload
                self._write_fut = self._loop._proactor.send(self._sock, payload)
                self._pending_write = len(payload)
                self._write_fut.add_done_callback(self._loop_writing)
                self._maybe_pause_protocol()
            else:
                if self._closing:
                    self._loop.call_soon(self._call_connection_lost, None)
                if self._eof_written:
                    self._sock.shutdown(socket.SHUT_WR)
                if self._empty_waiter is not None and not self._empty_waiter.done():
                    self._empty_waiter.set_result(None)

            # This callback can write/close/abort, so all state above must be
            # coherent before calling it and nothing may submit afterward.
            self._maybe_resume_protocol()
        except exceptions.CancelledError:
            self._force_close(ConnectionAbortedError("stream write cancelled"))
        except ConnectionResetError as exc:
            self._force_close(exc)
        except OSError as exc:
            self._fatal_error(exc, "Fatal write error on io_uring stream transport")


class IoUringSocketTransport(_PartialWriteMixin, proactor_events._ProactorSocketTransport):
    """TCP/Unix transport with io_uring byte-count write semantics."""

    def _call_connection_lost(self, exc):
        if self._called_connection_lost:
            return
        self._called_connection_lost = True
        try:
            self._protocol.connection_lost(exc)
        finally:
            # Linux sockets can already be disconnected after peer EOF. Closing
            # releases them without the Windows proactor's shutdown workaround.
            try:
                self._sock.close()
            finally:
                self._sock = None
                server, self._server = self._server, None
                if server is not None:
                    if sys.version_info >= (3, 13):
                        server._detach(self)
                    else:
                        server._detach()
