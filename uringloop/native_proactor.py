from asyncio import events, futures
from collections.abc import Buffer, Callable
from dataclasses import dataclass
import errno
from io import BufferedReader, IOBase
import os
import select
import socket
import time
from typing import Any
import weakref

from uringloop import _uringcore


@dataclass(slots=True)
class _Pending:
    future: "_NativeFuture | None"
    file: object
    handler: Callable[[Any], None] | None


class _NativeFuture(futures.Future[Any]):
    def __init__(self, proactor: "NativeIoUringProactor", *, loop: events.AbstractEventLoop | None):
        super().__init__(loop=loop)
        self._proactor_ref = weakref.ref(proactor)
        self._request: Any | None = None

    def cancel(self, msg: Any | None = None):
        if not self.done():
            proactor = self._proactor_ref()
            if proactor is not None:
                proactor._cancel_future(self)
        return super().cancel(msg=msg)


class NativeIoUringProactor:
    """Asyncio proactor backed by the native Phase 1 ring core."""

    def __init__(self, entries: int = 256, flags: int = 0):
        self._loop: events.AbstractEventLoop | None = None
        self._results: list[futures.Future[Any]] = []
        self._ring: Any | None = _uringcore.Ring(entries, flags=flags)
        self._cache: dict[Any, _Pending] = {}
        self._pending_submit = False
        self._stopped_serving: weakref.WeakSet[Any] = weakref.WeakSet()
        self._transports: weakref.WeakSet[Any] = weakref.WeakSet()
        self._transport_requests: set[Any] = set()

    def set_loop(self, loop: events.AbstractEventLoop):
        self._loop = loop

    def _check_closed(self):
        if self._ring is None:
            raise RuntimeError("IoUringProactor is closed")

    def _new_future(self) -> _NativeFuture:
        return _NativeFuture(self, loop=self._loop)

    def _prepare(self, method: Callable[..., Any], *args: Any, **kwargs: Any):
        self._check_closed()
        try:
            return method(*args, **kwargs)
        except BufferError:
            self._flush()
            return method(*args, **kwargs)

    def _register(
        self,
        request: Any,
        future: _NativeFuture | None,
        file: object,
        handler: Callable[[Any], None] | None,
    ):
        self._cache[request] = _Pending(future, file, handler)
        if future is not None:
            future._request = request
        self._pending_submit = True

    def _submit_native(
        self,
        request: Any,
        file: object,
        result_builder: Callable[[int], Any],
    ) -> _NativeFuture:
        future = self._new_future()

        def complete(completion: Any):
            if completion.result < 0:
                raise OSError(-completion.result, os.strerror(-completion.result))
            return result_builder(completion.result)

        self._register(request, future, file, lambda completion: self._resolve(future, complete, completion))
        return future

    def _resolve(self, future: _NativeFuture, callback: Callable[[Any], Any], value: Any):
        if future.done():
            return
        try:
            result = callback(value)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        if future.done():
            self._results.append(future)

    def _complete_immediately(self, future: _NativeFuture, callback: Callable[[], Any]):
        self._resolve(future, lambda ignored: callback(), None)

    def _arm_readiness(
        self,
        future: _NativeFuture,
        file: object,
        mask: int,
        action: Callable[[], Any],
    ):
        request = self._prepare(self._ring.prepare_poll, file, mask)

        def ready(completion: Any):
            if completion.result < 0:
                raise OSError(-completion.result, os.strerror(-completion.result))
            try:
                result = action()
            except (BlockingIOError, InterruptedError):
                self._arm_readiness(future, file, mask, action)
                return
            future.set_result(result)
            self._results.append(future)

        self._register(request, future, file, lambda completion: self._resolve_readiness(future, ready, completion))

    def _resolve_readiness(
        self,
        future: _NativeFuture,
        callback: Callable[[Any], None],
        completion: Any,
    ):
        if future.done():
            return
        try:
            callback(completion)
        except BaseException as exc:
            future.set_exception(exc)
            self._results.append(future)

    def _readiness_operation(
        self,
        file: object,
        mask: int,
        action: Callable[[], Any],
    ) -> _NativeFuture:
        future = self._new_future()
        try:
            result = action()
        except (BlockingIOError, InterruptedError):
            self._arm_readiness(future, file, mask, action)
        except BaseException as exc:
            future.set_exception(exc)
            self._results.append(future)
        else:
            future.set_result(result)
            self._results.append(future)
        return future

    def _flush(self):
        if self._pending_submit:
            self._ring.submit()
            self._pending_submit = False

    def select(self, timeout: float | None = None):
        if not self._results:
            self._poll(timeout)
        results = self._results
        self._results = []
        return results

    def select_batch(self, timeout: float | None, max_events: int):
        if max_events < 1:
            raise ValueError("max_events must be positive")
        if not self._results:
            self._check_closed()
            self._flush()
            timeout_ns = None if timeout is None else max(0, int(timeout * 1_000_000_000))
            for completion in self._ring.wait(timeout_ns=timeout_ns, max_completions=max_events):
                self._handle_completion(completion)
        results = self._results[:max_events]
        del self._results[:max_events]
        immediate = bool(self._results) or self._ring.cq_ready or self._ring.cq_overflowed
        return results, immediate

    def recv(self, conn: socket.socket | IOBase, nbytes: int, flags: int = 0):
        buffer = bytearray(nbytes)
        if isinstance(conn, socket.socket):
            request = self._prepare(self._ring.prepare_recv, conn, buffer, flags)
        else:
            request = self._prepare(self._ring.prepare_read, conn, buffer, -1)
        return self._submit_native(request, conn, lambda size: bytes(memoryview(buffer)[:size]))

    def recv_into(self, conn: socket.socket | IOBase, buffer: Buffer, flags: int = 0):
        if isinstance(conn, socket.socket):
            request = self._prepare(self._ring.prepare_recv, conn, buffer, flags)
        else:
            request = self._prepare(self._ring.prepare_read, conn, buffer, -1)
        return self._submit_native(request, conn, int)

    def send(self, conn: socket.socket | IOBase, buffer: Buffer, flags: int = 0):
        view = memoryview(buffer)
        if isinstance(conn, socket.socket):
            request = self._prepare(self._ring.prepare_send, conn, view, flags)
        else:
            request = self._prepare(self._ring.prepare_write, conn, view, -1)
        return self._submit_native(request, conn, int)

    def register_transport(self, transport: Any):
        """Track native transports weakly for proactor shutdown."""
        self._check_closed()
        self._transports.add(transport)

    def _submit_transport(self, prepare: Callable[..., Any], conn: Any, buffer: Buffer, callback: Callable[[Any], None]):
        if not callable(callback):
            raise TypeError("transport completion callback must be callable")
        # Allocate the GC-tracked record before prepare publishes an SQE.
        pending = _Pending(None, conn, callback)
        request = self._prepare(prepare, conn, buffer)
        self._pending_submit = True
        try:
            self._cache[request] = pending
            self._transport_requests.add(request)
        except BaseException:
            # Dict/set growth can fail after prepare. An unregistered fd SQE
            # must never survive the caller's subsequent socket cleanup. The
            # native rollback changes it to NOP without allocating or releasing
            # its CQE identity; the eventual unregistered completion is ignored.
            self._ring.abandon_prepared(request)
            self._cache.pop(request, None)
            self._transport_requests.discard(request)
            raise
        return request

    def recv_transport(self, conn: Any, buffer: Buffer, callback: Callable[[Any], None]):
        """Prepare one stream receive without an intermediate Future."""
        self._check_closed()
        return self._submit_transport(self._ring.prepare_recv, conn, buffer, callback)

    def send_transport(self, conn: Any, buffer: Buffer, callback: Callable[[Any], None]):
        """Prepare one counted stream send without an intermediate Future."""
        self._check_closed()
        return self._submit_transport(self._ring.prepare_send, conn, buffer, callback)

    def cancel_transport(self, request: Any):
        """Cancel a transport request, retaining its callback until retirement."""
        self._check_closed()
        if request not in self._transport_requests or request not in self._cache:
            return
        if request.state == "prepared":
            self._flush()
        try:
            cancellation = self._prepare(self._ring.prepare_cancel, request)
        except (RuntimeError, ValueError):
            return

        def cancellation_complete(completion: Any):
            if completion.result >= 0 or completion.result == -errno.ENOENT:
                return
            error = OSError(-completion.result, os.strerror(-completion.result))
            if self._loop is not None:
                self._loop.call_exception_handler(
                    {"message": "Cancelling a native transport request failed", "exception": error, "request": request}
                )

        self._register(cancellation, None, self._cache[request].file, cancellation_complete)

    def accept(self, listener: socket.socket):
        def action():
            connection, address = listener.accept()
            connection.setblocking(False)
            return connection, address

        return self._readiness_operation(listener, select.POLLIN | select.POLLERR | select.POLLHUP, action)

    def connect(self, conn: socket.socket, address: Any):
        result = conn.connect_ex(address)
        if result in (0, errno.EISCONN):
            future = self._new_future()
            future.set_result(None)
            self._results.append(future)
            return future
        if result not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR):
            future = self._new_future()
            future.set_exception(OSError(result, os.strerror(result)))
            self._results.append(future)
            return future

        def action():
            error = conn.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                raise BlockingIOError(error, os.strerror(error))
            if error:
                raise OSError(error, os.strerror(error))
            return None

        future = self._new_future()
        self._arm_readiness(
            future,
            conn,
            select.POLLOUT | select.POLLERR | select.POLLHUP,
            action,
        )
        return future

    def sendto(self, conn: socket.socket, buffer: Buffer, flags: int = 0, addr: Any | None = None):
        view = memoryview(buffer)

        def action():
            if addr is None:
                return conn.send(view, flags)
            return conn.sendto(view, flags, addr)

        return self._readiness_operation(conn, select.POLLOUT | select.POLLERR, action)

    def recvfrom(self, conn: socket.socket, nbytes: int, flags: int = 0):
        return self._readiness_operation(
            conn,
            select.POLLIN | select.POLLERR,
            lambda: conn.recvfrom(nbytes, flags),
        )

    def recvfrom_into(self, conn: socket.socket, buffer: Buffer, flags: int = 0):
        return self._readiness_operation(
            conn,
            select.POLLIN | select.POLLERR,
            lambda: conn.recvfrom_into(buffer, len(memoryview(buffer)), flags),
        )

    def sendfile(self, sock: socket.socket, file: BufferedReader, offset: int, count: int):
        sent = 0

        def action():
            nonlocal sent
            while sent < count:
                chunk = os.sendfile(sock.fileno(), file.fileno(), offset + sent, count - sent)
                if chunk == 0:
                    return sent
                sent += chunk
            if sent < count:
                raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))
            return sent

        return self._readiness_operation(sock, select.POLLOUT | select.POLLERR | select.POLLHUP, action)

    def poll_add(self, file: socket.socket | IOBase | int, poll_mask: int):
        request = self._prepare(self._ring.prepare_poll, file, poll_mask)
        return self._submit_native(request, file, int)

    def _cancel_future(self, future: _NativeFuture):
        request = future._request
        if request is None or request not in self._cache:
            return
        if request.state == "prepared":
            self._flush()
        try:
            cancellation = self._prepare(self._ring.prepare_cancel, request)
        except (RuntimeError, ValueError):
            return

        def cancellation_complete(completion: Any):
            if completion.result >= 0 or completion.result == -errno.ENOENT:
                return
            error = OSError(-completion.result, os.strerror(-completion.result))
            if self._loop is None:
                raise error
            self._loop.call_exception_handler(
                {
                    "message": "Cancelling a native io_uring request failed",
                    "exception": error,
                    "future": future,
                }
            )

        self._register(cancellation, None, self._cache[request].file, cancellation_complete)

    def _handle_completion(self, completion: Any):
        pending = self._cache.get(completion.request)
        if pending is None:
            return
        if completion.final:
            self._cache.pop(completion.request, None)
            self._transport_requests.discard(completion.request)
        if pending.future is None:
            if pending.handler is not None:
                pending.handler(completion)
            return
        if pending.future.cancelled():
            return
        if pending.file in self._stopped_serving:
            pending.future.cancel()
            return
        if pending.handler is not None:
            pending.handler(completion)

    def _poll(self, timeout: float | None = None):
        self._check_closed()
        self._flush()
        timeout_ns = None if timeout is None else max(0, int(timeout * 1_000_000_000))
        completions = self._ring.wait(timeout_ns=timeout_ns)
        for completion in completions:
            self._handle_completion(completion)
        while self._ring.cq_ready or self._ring.cq_overflowed:
            for completion in self._ring.reap():
                self._handle_completion(completion)

    def _stop_serving(self, file: Any):
        self._stopped_serving.add(file)
        for pending in list(self._cache.values()):
            if pending.file is file and pending.future is not None:
                pending.future.cancel()

    def close(self):
        if self._ring is None:
            return
        ring = self._ring
        transport_error = None
        try:
            for transport in list(self._transports):
                try:
                    close_transport = getattr(transport, "_close_for_loop", transport.abort)
                    close_transport()
                except BaseException as error:
                    # Finish every owned transport and the ring before raising
                    # the first shutdown failure; one callback cannot leak peers.
                    if transport_error is None:
                        transport_error = error
            for pending in list(self._cache.values()):
                if pending.future is not None and not pending.future.done():
                    pending.future.cancel()
            deadline = time.monotonic() + 1
            while self._cache and time.monotonic() < deadline:
                self._poll(0.05)
        finally:
            ring.close()
            self._ring = None
            self._cache.clear()
            self._results = []
            self._transport_requests.clear()
            self._transports.clear()
        if transport_error is not None:
            raise transport_error

    def __del__(self):
        try:
            self.close()
        except BaseException:
            pass
