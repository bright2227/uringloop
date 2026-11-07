from asyncio import events, futures
from collections.abc import Buffer, Callable
from dataclasses import dataclass
import errno
from io import BufferedReader, IOBase
import itertools
import os
import socket
import time
from typing import Any, Self, TypeAlias
import weakref
from weakref import WeakSet

from uringloop._types import IoUring, IoUringCqe, Sockaddr, pyAddress
from uringloop.lib import (
    IOSQE_IO_HARDLINK,
    OFFSET_CURRENT_POS,
    io_uring_cqe_seen,
    io_uring_get_sqe,
    io_uring_peek_cqe,
    io_uring_prep_accept,
    io_uring_prep_cancel64,
    io_uring_prep_connect,
    io_uring_prep_poll_add,
    io_uring_prep_read,
    io_uring_prep_recv,
    io_uring_prep_recvfrom,
    io_uring_prep_send,
    io_uring_prep_sendto,
    io_uring_prep_splice,
    io_uring_prep_write,
    io_uring_queue_exit,
    io_uring_queue_init,
    io_uring_sq_ready,
    io_uring_sq_space_left,
    io_uring_sqe_set_data64,
    io_uring_sqe_set_flags,
    io_uring_submit,
    io_uring_wait_cqe,
    io_uring_wait_cqe_timeout,
    new_io_uring,
    new_kernel_timespec,
    new_readable_sockaddr,
    new_writable_sockaddr,
)
from uringloop.log import logger
from uringloop.operation import (
    AcceptOperation,
    BaseOperation,
    ConnectOperation,
    PollAddOperation,
    ReadIntoOperation,
    ReadOperation,
    RecvFromIntoOperation,
    RecvFromOperation,
    RecvIntoOperation,
    RecvOperation,
    SendOperation,
    SendToOperation,
    SendfileOperation,
    WriteOperation,
    get_os_error,
)
from uringloop.request import (
    AcceptRequest,
    Cancel64Request,
    ConnectRequest,
    KernelRequest,
    PollAddRequest,
    ReadRequest,
    RecvFromRequest,
    RecvRequest,
    SendRequest,
    SendToRequest,
    SpliceRequest,
    WriteRequest,
)


@dataclass(slots=True)
class PendingCompletion:
    operation: BaseOperation
    future: "_IoUringFuture | None"
    request: KernelRequest


ProactorCache: TypeAlias = dict[int, PendingCompletion]


DEFAULT_ENTRIES = 256


class _IoUringFuture(futures.Future[Any]):
    def __init__(self, proactor: "IoUringProactor", operation: BaseOperation, *, loop: events.AbstractEventLoop | None = None):
        super().__init__(loop=loop)
        if self._source_traceback:  # type: ignore[reportUnknownMemberType]
            del self._source_traceback[-1]  # type: ignore[reportUnknownMemberType]
        self._proactor_ref = weakref.ref(proactor)
        self._operation = operation

    def _cancel(self):
        proactor = self._proactor_ref()
        if not proactor:
            return
        proactor.cancel_operation(self._operation)

    def cancel(self, msg: Any | None = None):
        # avoid double cancel
        if not self.cancelled():
            self._cancel()
        return super().cancel(msg=msg)


class _ProactorSubmit:
    def __init__(self, ring: IoUring, cache: ProactorCache) -> None:
        self._iouring = ring
        self._cache: ProactorCache = cache
        self._unsubmitted: list[tuple[int, KernelRequest]] = []
        self._pending_submit = False

    def _get_sqe(self) -> Any:
        sqe = io_uring_get_sqe(self._iouring)
        if sqe is None:
            # submission queue is full: flush the prepared SQEs to the kernel
            # to free up slots, then retry
            self.flush()
            sqe = io_uring_get_sqe(self._iouring)
            if sqe is None:
                raise RuntimeError("io_uring submission queue is full")
        return sqe

    def ensure_capacity(self, count: int) -> None:
        """Ensure a group of linked SQEs fits in one submission."""
        if io_uring_sq_space_left(self._iouring) < count:
            self.flush()
        if io_uring_sq_space_left(self._iouring) < count:
            raise RuntimeError(f"io_uring submission queue cannot fit {count} linked entries")

    def _prep(
        self,
        prep_fn: Callable[[Any, Any], None],
        request: KernelRequest,
        user_data: int,
        flags: int,
    ) -> Self:
        sqe = self._get_sqe()
        prep_fn(sqe, request)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmitted.append((user_data, request))
        return self

    def recv(self, request: RecvRequest, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_recv, request, user_data, flags)

    def read(self, request: ReadRequest, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_read, request, user_data, flags)

    def recvfrom(self, request: RecvFromRequest, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_recvfrom, request, user_data, flags)

    def sendto(self, request: SendToRequest, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_sendto, request, user_data, flags)

    def send(self, request: SendRequest, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_send, request, user_data, flags)

    def write(self, request: WriteRequest, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_write, request, user_data, flags)

    def accept(self, request: AcceptRequest, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_accept, request, user_data, flags)

    def connect(self, request: ConnectRequest, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_connect, request, user_data, flags)

    def splice(self, request: SpliceRequest, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_splice, request, user_data, flags)

    def cancel(self, request: Cancel64Request, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_cancel64, request, user_data, flags)

    def poll_add(self, request: PollAddRequest, user_data: int, flags: int = 0) -> Self:
        return self._prep(io_uring_prep_poll_add, request, user_data, flags)

    def submit(self, op: BaseOperation, fut: _IoUringFuture | None):
        """Register the prepared SQEs; the syscall is deferred to flush().

        Batching all SQEs prepared between two polls into one
        io_uring_submit call is what makes io_uring cheaper than epoll:
        one io_uring_enter per loop iteration instead of one per operation.
        """
        for user_data, request in self._unsubmitted:
            self._cache[user_data] = PendingCompletion(op, fut, request)
        self._unsubmitted = []
        self._pending_submit = True

    def flush(self):
        while self._pending_submit:
            try:
                submitted = io_uring_submit(self._iouring)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
            if io_uring_sq_ready(self._iouring) == 0:
                self._pending_submit = False
            elif submitted == 0:
                raise RuntimeError("io_uring made no progress submitting queued entries")


class IoUringProactor:
    """Proactor implementation using io uring."""

    def __init__(self, entries: int = DEFAULT_ENTRIES, flags: int = 0):
        self._loop: events.AbstractEventLoop | None = None
        self._results: list[futures.Future[Any]] = []
        self._iouring: IoUring | None = None
        ring = new_io_uring()
        io_uring_queue_init(entries, ring, flags)

        self._iouring = ring
        self._cache: ProactorCache = {}
        self._stopped_serving: WeakSet[Any] = weakref.WeakSet()
        self.submitter = _ProactorSubmit(self._iouring, self._cache)
        # unique per-operation key for the SQE user_data field; an object id()
        # must not be used here because CPython reuses addresses of freed
        # objects, which would collide with still-inflight operations
        self._user_data_counter = itertools.count(1)

    def _next_user_data(self) -> int:
        return next(self._user_data_counter)

    def _check_closed(self):
        if self._iouring is None:
            raise RuntimeError("IoUringProactor is closed")

    def set_loop(self, loop: events.AbstractEventLoop):
        self._loop = loop

    def select(self, timeout: float | None = None):
        if not self._results:
            self._poll(timeout)
        tmp = self._results
        self._results = []
        return tmp

    def recv(self, conn: socket.socket | IOBase, nbytes: int, flags: int = 0) -> futures.Future[bytes]:
        buf = bytearray(nbytes)
        user_data = self._next_user_data()
        if isinstance(conn, socket.socket):
            request = RecvRequest(sock=conn, buffer=buf, flags=flags)
            op = RecvOperation(sock=conn, buffer=buf, flags=flags, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.recv(request, user_data).submit(op=op, fut=fut)
            return fut
        else:
            request = ReadRequest(file=conn, buffer=buf, offset=OFFSET_CURRENT_POS)
            op = ReadOperation(file=conn, buffer=buf, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.read(request, user_data).submit(op=op, fut=fut)
            return fut

    def recv_into(self, conn: socket.socket | IOBase, buf: Buffer, flags: int = 0) -> futures.Future[int]:
        user_data = self._next_user_data()
        if isinstance(conn, socket.socket):
            request = RecvRequest(sock=conn, buffer=buf, flags=flags)
            op = RecvIntoOperation(sock=conn, buffer=buf, flags=flags, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.recv(request, user_data).submit(op=op, fut=fut)
            return fut
        else:
            request = ReadRequest(file=conn, buffer=buf, offset=OFFSET_CURRENT_POS)
            op = ReadIntoOperation(file=conn, buffer=buf, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.read(request, user_data).submit(op=op, fut=fut)
            return fut

    def recvfrom(self, conn: socket.socket, nbytes: int, flags: int = 0) -> futures.Future[tuple[bytes, pyAddress]]:
        buf = bytearray(nbytes)
        sockaddr = new_writable_sockaddr(conn.family)
        request = RecvFromRequest(sock=conn, buffer=buf, sockaddr=sockaddr, msghdr_flags=0, flags=flags)
        user_data = self._next_user_data()
        op = RecvFromOperation(sock=conn, buffer=buf, sockaddr=sockaddr, flags=flags, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.recvfrom(request, user_data).submit(op=op, fut=fut)
        return fut

    def recvfrom_into(self, conn: socket.socket, buf: Buffer, flags: int = 0) -> futures.Future[tuple[int, pyAddress]]:
        sockaddr = new_writable_sockaddr(conn.family)
        request = RecvFromRequest(sock=conn, buffer=buf, sockaddr=sockaddr, msghdr_flags=0, flags=flags)
        user_data = self._next_user_data()
        op = RecvFromIntoOperation(sock=conn, buffer=buf, sockaddr=sockaddr, flags=flags, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.recvfrom(request, user_data).submit(op=op, fut=fut)
        return fut

    def sendto(self, conn: socket.socket, buf: Buffer, flags: int = 0, addr: pyAddress | None = None) -> futures.Future[int]:
        sockaddr: Sockaddr | None = None
        if addr:
            sockaddr = new_readable_sockaddr(conn.family, addr)
        request = SendToRequest(sock=conn, buffer=buf, sockaddr=sockaddr, flags=flags, msghdr_flags=0)
        user_data = self._next_user_data()
        op = SendToOperation(sock=conn, buffer=buf, sockaddr=sockaddr, flags=flags, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.sendto(request, user_data).submit(op=op, fut=fut)
        return fut

    def send(self, conn: socket.socket | IOBase, buf: Buffer, flags: int = 0) -> futures.Future[int]:
        buf_view = memoryview(buf)
        user_data = self._next_user_data()
        if isinstance(conn, socket.socket):
            request = SendRequest(sock=conn, buffer=buf_view, flags=flags)
            op = SendOperation(sock=conn, buffer=buf_view, flags=flags, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.send(request, user_data).submit(op=op, fut=fut)
            return fut
        else:
            request = WriteRequest(file=conn, buffer=buf_view, offset=OFFSET_CURRENT_POS)
            op = WriteOperation(file=conn, buffer=buf_view, offset=OFFSET_CURRENT_POS, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.write(request, user_data).submit(op=op, fut=fut)
            return fut

    def accept(self, listener: socket.socket) -> futures.Future[tuple[socket.socket, pyAddress]]:
        flags = socket.SOCK_CLOEXEC
        sockaddr = new_writable_sockaddr(listener.family)
        request = AcceptRequest(sock=listener, sockaddr=sockaddr, flags=flags)
        user_data = self._next_user_data()
        op = AcceptOperation(sock=listener, sockaddr=sockaddr, flags=flags, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.accept(request, user_data).submit(op=op, fut=fut)
        return fut

    def connect(self, conn: socket.socket, address: pyAddress) -> futures.Future[None]:
        addr = new_readable_sockaddr(family=conn.family, address=address)
        request = ConnectRequest(sock=conn, sockaddr=addr)
        user_data = self._next_user_data()
        op = ConnectOperation(sock=conn, sockaddr=addr, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.connect(request, user_data).submit(op=op, fut=fut)
        return fut

    def sendfile(self, sock: socket.socket, file: BufferedReader, offset: int, count: int) -> futures.Future[int]:
        """NOTE: edge cases would be handled by event loop"""
        # Linked SQEs cannot cross submission boundaries.
        self.submitter.ensure_capacity(2)
        pipe_r, pipe_w = os.pipe()

        f2p_request = SpliceRequest(
            file_in=file,
            off_in=offset,
            file_out=pipe_w,
            off_out=-1,
            nbytes=count,
            splice_flags=0,
        )
        f2p_user_data = self._next_user_data()
        p2s_request = SpliceRequest(
            file_in=pipe_r,
            off_in=-1,
            file_out=sock,
            off_out=-1,
            nbytes=count,
            splice_flags=0,
        )
        p2s_user_data = self._next_user_data()
        op = SendfileOperation(
            sock=sock,
            file=file,
            pipe_w=pipe_w,
            pipe_r=pipe_r,
            offset=offset,
            count=count,
            f2p_user_data=f2p_user_data,
            f2p_done=False,
            p2s_user_data=p2s_user_data,
        )
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.splice(f2p_request, f2p_user_data, flags=IOSQE_IO_HARDLINK).splice(p2s_request, p2s_user_data).submit(
            op=op, fut=fut
        )
        return fut

    def cancel_operation(self, operation: BaseOperation, flags: int = 0):
        target_user_data = operation.get_user_data()
        if not target_user_data:
            return
        request = Cancel64Request(user_data=target_user_data, flags=flags)
        self.submitter.cancel(request, self._next_user_data()).submit(operation, None)

    def poll_add(self, file: socket.socket | IOBase | int, poll_mask: int) -> futures.Future[int]:
        request = PollAddRequest(file=file, poll_mask=poll_mask)
        user_data = self._next_user_data()

        op = PollAddOperation(file=file, poll_mask=poll_mask, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.poll_add(request, user_data).submit(op=op, fut=fut)
        return fut

    def _poll(self, timeout: float | None = None):
        # push every SQE prepared since the last poll to the kernel in a
        # single syscall before waiting for completions
        self.submitter.flush()

        if timeout is None:
            while True:
                try:
                    cqe = io_uring_wait_cqe(self._iouring)
                    break
                except OSError as e:
                    # interrupted by a signal: retry; a raised signal handler
                    # exception (e.g. KeyboardInterrupt) still propagates
                    if e.errno == errno.EINTR:
                        continue
                    raise
            self._handle_cqe(cqe)
            io_uring_cqe_seen(self._iouring, cqe)

        elif timeout > 0:
            seconds = int(timeout)
            fractional_seconds = timeout - seconds
            nanoseconds = int(fractional_seconds * 1e9)
            ktspec = new_kernel_timespec(tv_sec=seconds, tv_nsec=nanoseconds)
            try:
                cqe = io_uring_wait_cqe_timeout(self._iouring, ktspec)
            except OSError as e:
                # treat a signal interruption like a timeout: the event loop
                # recalculates the timeout and calls select again
                if e.errno in (errno.ETIME, errno.EINTR):
                    return
                raise
            self._handle_cqe(cqe)
            io_uring_cqe_seen(self._iouring, cqe)

        while True:
            try:
                cqe = io_uring_peek_cqe(self._iouring)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    return
                raise e

            self._handle_cqe(cqe)
            io_uring_cqe_seen(self._iouring, cqe)

    def _stop_serving(self, obj: Any):
        self._stopped_serving.add(obj)
        # iterate over a copy: fut.cancel() submits a cancel SQE, which
        # registers a new cache entry and would break dict iteration
        for pending in list(self._cache.values()):
            op = pending.operation
            fut = pending.future
            if op.get_file_obj() in self._stopped_serving and fut and not fut.done():
                fut.cancel()

    def _handle_cqe(self, cqe: IoUringCqe):
        try:
            pending = self._cache.pop(cqe.user_data)
            # TODO: consider if there is more cqe with the same user_data, e.g. multishot.
        except KeyError:
            if self._loop is not None and self._loop.get_debug():
                self._loop.call_exception_handler(
                    {
                        "message": ("_poll returned an unexpected event"),
                        "status": (
                            "err=%s res=%s user_data=%#x"
                            % (cqe.res, os.strerror(-cqe.res) if cqe.res < 0 else "", cqe.user_data)
                        ),
                    }
                )
            return

        op = pending.operation
        fut = pending.future
        if fut:
            op.mark_seen(cqe.user_data)
            # TODO: figure out the correct way to _stopped_serving, may be io_uring_prep_cancel_fd?
            if op.get_file_obj() in self._stopped_serving:
                # the self.cancel_operation would be triggered
                # if the user_data is seen, the op.get_user_data would not appeared
                fut.cancel()
            else:
                self._run_operation(cqe, op, fut)
        # if fut is None, it means it from cancelation
        elif cqe.res < 0:
            if self._loop is None or cqe.res == -errno.ENOENT and op.all_seen():
                # if  target cqe completed before the cancelation, ignores the cancelation "not found"
                # TODO: if the cancelation cqe returns earlier than target cqe, avoid it shows error message.
                return
            else:
                context: dict[str, Any] = {"message": f"Cancelling a {op} failed", "exception": get_os_error(cqe.res)}
                self._loop.call_exception_handler(context)

    def _run_operation(self, cqe: IoUringCqe, op: BaseOperation, fut: _IoUringFuture):
        if fut.done():
            return
        op.operate(cqe, fut)
        if fut.done():
            self._results.append(fut)

    def close(self):
        if self._iouring is None:
            # already closed
            return

        # Cancel remaining registered operations.
        for pending in list(self._cache.values()):
            fut = pending.future
            if not fut:
                # Nothing to do with cancelled futures
                continue

            try:
                fut.cancel()
            except OSError as exc:
                if self._loop is not None:
                    context: dict[str, Any] = {
                        "message": "Cancelling a _IoUringFuture failed",
                        "exception": exc,
                        "future": fut,
                    }
                    if fut._source_traceback:  # type: ignore[reportUnknownMemberType]
                        context["source_traceback"] = fut._source_traceback  # type: ignore[reportUnknownMemberType]
                    self._loop.call_exception_handler(context)

        msg_update = 1.0
        start_time = time.monotonic()
        next_msg = start_time + msg_update
        while self._cache:
            if next_msg <= time.monotonic():
                logger.debug("%r is running after closing for %.1f seconds", self, time.monotonic() - start_time)
                next_msg = time.monotonic() + msg_update

            self._poll(msg_update)

        io_uring_queue_exit(self._iouring)
        self._results = []
        self._iouring = None

    def __del__(self):
        self.close()
