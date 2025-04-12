from asyncio import events, futures
from collections.abc import Buffer
import errno
from io import BufferedReader, IOBase
import os
import socket
import time
from typing import Any, Self, TypeAlias
import weakref
from weakref import WeakSet

from uringloop.lib import (
    IOSQE_IO_HARDLINK,
    AcceptKwargs,
    Cancel64Kwargs,
    ConnectKwargs,
    IoUring,
    IoUringCqe,
    PollAddKwargs,
    ReadKwargs,
    RecvFromKwargs,
    RecvKwargs,
    SendKwargs,
    SendToKwargs,
    Sockaddr,
    SpliceKwargs,
    WriteKwargs,
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
    io_uring_sqe_set_data64,
    io_uring_sqe_set_flags,
    io_uring_submit,
    io_uring_wait_cqe,
    io_uring_wait_cqe_timeout,
    new_io_uring,
    new_kernel_timespec,
    new_readable_sockaddr,
    new_writable_sockaddr,
    pyAddress,
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


ProatorCache: TypeAlias = dict[int, tuple[BaseOperation, "_IoUringFuture | None"]]


DEFAULT_ENTRIES = 16


class _IoUringFuture(futures.Future[Any]):
    def __init__(self, proactor: "IoUringProactor", operation: BaseOperation, *, loop: events.AbstractEventLoop | None = None):
        super().__init__(loop=loop)
        if self._source_traceback:   # type: ignore[reportUnknownMemberType]
            del self._source_traceback[-1]   # type: ignore[reportUnknownMemberType]
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
    def __init__(self, ring: IoUring, cache: ProatorCache) -> None:
        self._iouring = ring
        self._cache: ProatorCache = cache
        self._unsubmit_user_datas: list[int] = []

    def recv(self, kwargs: RecvKwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_recv(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def read(self, kwargs: ReadKwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_read(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def recvfrom(self, kwargs: RecvFromKwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_recvfrom(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def sendto(self, kwargs: SendToKwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_sendto(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def send(self, kwargs: SendKwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_send(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def write(self, kwargs: WriteKwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_write(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def accept(self, kwargs: AcceptKwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_accept(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def connect(self, kwargs: ConnectKwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_connect(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def splice(self, kwargs: SpliceKwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_splice(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def cancel(self, kwargs: Cancel64Kwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_cancel64(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def poll_add(self, kwargs: PollAddKwargs, flags: int = 0) -> Self:
        sqe = io_uring_get_sqe(self._iouring)
        io_uring_prep_poll_add(sqe, **kwargs)
        user_data = id(kwargs)
        io_uring_sqe_set_data64(sqe, user_data)
        if flags:
            io_uring_sqe_set_flags(sqe, flags)
        self._unsubmit_user_datas.append(user_data)
        return self

    def submit(self, op: BaseOperation, fut: _IoUringFuture | None):
        for user_data in self._unsubmit_user_datas:
            self._cache[user_data] = (op, fut)
        io_uring_submit(self._iouring)
        self._unsubmit_user_datas = []


class IoUringProactor:
    """Proactor implementation using io uring."""

    def __init__(self, entries: int = DEFAULT_ENTRIES, flags: int = 0):
        self._loop: events.AbstractEventLoop | None = None
        self._results: list[futures.Future[Any]] = []
        ring = new_io_uring()
        io_uring_queue_init(entries, ring, flags)

        self._iouring = ring
        self._cache: ProatorCache = {}
        self._stopped_serving: WeakSet[Any] = weakref.WeakSet()
        self.submitter = _ProactorSubmit(self._iouring, self._cache)

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
        try:
            return tmp
        finally:
            tmp = None

    def recv(self, conn: socket.socket | IOBase, nbytes: int, flags: int = 0) -> futures.Future[bytes]:
        buf = bytearray(nbytes)
        if isinstance(conn, socket.socket):
            kwargs = RecvKwargs(sock=conn, buffer=buf, flags=flags)
            user_data = id(kwargs)
            op = RecvOperation(sock=conn, buffer=buf, flags=flags, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.recv(kwargs).submit(op=op, fut=fut)
            return fut
        else:
            kwargs = ReadKwargs(file=conn, buffer=buf, offset=0)
            user_data = id(kwargs)
            op = ReadOperation(file=conn, buffer=buf, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.read(kwargs).submit(op=op, fut=fut)
            return fut

    def recv_into(self, conn: socket.socket | IOBase, buf: Buffer, flags: int = 0) -> futures.Future[int]:
        if isinstance(conn, socket.socket):
            kwargs = RecvKwargs(sock=conn, buffer=buf, flags=flags)
            user_data = id(kwargs)
            op = RecvIntoOperation(sock=conn, buffer=buf, flags=flags, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.recv(kwargs).submit(op=op, fut=fut)
            return fut
        else:
            kwargs = ReadKwargs(file=conn, buffer=buf, offset=0)
            user_data = id(kwargs)
            op = ReadIntoOperation(file=conn, buffer=buf, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.read(kwargs).submit(op=op, fut=fut)
            return fut

    def recvfrom(self, conn: socket.socket, nbytes: int, flags: int = 0) -> futures.Future[tuple[bytes, pyAddress]]:
        buf = bytearray(nbytes)
        sockaddr = new_writable_sockaddr(conn.family)
        kwargs = RecvFromKwargs(sock=conn, buffer=buf, sockaddr=sockaddr, msghdr_flags=0, flags=flags)
        user_data = id(kwargs)
        op = RecvFromOperation(sock=conn, buffer=buf, sockaddr=sockaddr, flags=flags, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.recvfrom(kwargs).submit(op=op, fut=fut)
        return fut

    def recvfrom_into(self, conn: socket.socket, buf: Buffer, flags: int = 0) -> futures.Future[tuple[int, pyAddress]]:
        sockaddr = new_writable_sockaddr(conn.family)
        kwargs = RecvFromKwargs(sock=conn, buffer=buf, sockaddr=sockaddr, msghdr_flags=0, flags=flags)
        user_data = id(kwargs)
        op = RecvFromIntoOperation(sock=conn, buffer=buf, sockaddr=sockaddr, flags=flags, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.recvfrom(kwargs).submit(op=op, fut=fut)
        return fut

    def sendto(self, conn: socket.socket, buf: Buffer, flags: int = 0, addr: pyAddress | None = None) -> futures.Future[int]:
        sockaddr: Sockaddr | None = None
        if addr:
            sockaddr = new_readable_sockaddr(conn.family, addr)
        kwargs = SendToKwargs(sock=conn, buffer=buf, sockaddr=sockaddr, flags=flags, msghdr_flags=0)
        user_data = id(kwargs)
        op = SendToOperation(sock=conn, buffer=buf, sockaddr=sockaddr, flags=flags, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.sendto(kwargs).submit(op=op, fut=fut)
        return fut

    def send(self, conn: socket.socket | IOBase, buf: Buffer, flags: int = 0) -> futures.Future[int]:
        buf_view = memoryview(buf)
        if isinstance(conn, socket.socket):
            kwargs = SendKwargs(sock=conn, buffer=buf_view, flags=flags)
            user_data = id(kwargs)
            op = SendOperation(sock=conn, buffer=buf_view, flags=flags, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.send(kwargs).submit(op=op, fut=fut)
            return fut
        else:
            kwargs = WriteKwargs(file=conn, buffer=buf_view, offset=0)
            user_data = id(kwargs)
            op = WriteOperation(file=conn, buffer=buf_view, offset=0, user_data=user_data)
            fut = _IoUringFuture(self, op, loop=self._loop)
            self.submitter.write(kwargs).submit(op=op, fut=fut)
            return fut

    def accept(self, listener: socket.socket) -> futures.Future[tuple[socket.socket, pyAddress]]:
        flags = 0
        sockaddr = new_writable_sockaddr(listener.family)
        kwargs = AcceptKwargs(sock=listener, sockaddr=sockaddr, flags=flags)
        user_data = id(kwargs)
        op = AcceptOperation(sock=listener, sockaddr=sockaddr, flags=flags, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.accept(kwargs).submit(op=op, fut=fut)
        return fut

    def connect(self, conn: socket.socket, address: pyAddress) -> futures.Future[None]:
        addr = new_readable_sockaddr(family=conn.family, address=address)
        kwargs = ConnectKwargs(sock=conn, sockaddr=addr)
        user_data = id(kwargs)
        op = ConnectOperation(sock=conn, sockaddr=addr, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.connect(kwargs).submit(op=op, fut=fut)
        return fut

    def sendfile(self, sock: socket.socket, file: BufferedReader, offset: int, count: int) -> futures.Future[int]:
        """NOTE: edge cases would be handled by event loop"""
        pipe_r, pipe_w = os.pipe()

        f2p_kwargs = SpliceKwargs(file_in=file, off_in=offset, file_out=pipe_w, off_out=-1, nbytes=count, splice_flags=0)
        f2p_user_data = id(f2p_kwargs)
        p2s_kwargs = SpliceKwargs(file_in=pipe_r, off_in=-1, file_out=sock, off_out=-1, nbytes=count, splice_flags=0)
        p2s_user_data = id(p2s_kwargs)
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
        self.submitter.splice(f2p_kwargs, flags=IOSQE_IO_HARDLINK).splice(p2s_kwargs).submit(op=op, fut=fut)
        return fut

    def cancel_operation(self, operation: BaseOperation, flags: int = 0):
        user_data = operation.get_user_data()
        if not user_data:
            return
        kwargs = Cancel64Kwargs(user_data=user_data, flags=flags)
        self.submitter.cancel(kwargs).submit(operation, None)

    def poll_add(self, file: socket.socket | IOBase | int, poll_mask: int) -> futures.Future[int]:
        kwargs = PollAddKwargs(file=file, poll_mask=poll_mask)
        user_data = id(kwargs)

        op = PollAddOperation(file=file, poll_mask=poll_mask, user_data=user_data)
        fut = _IoUringFuture(self, op, loop=self._loop)
        self.submitter.poll_add(kwargs).submit(op=op, fut=fut)
        return fut

    def _poll(self, timeout: float | None = None):
        if timeout is None:
            cqe = io_uring_wait_cqe(self._iouring)
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
                if e.errno == errno.ETIME:
                    return
                raise e
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
        for op, fut in self._cache.values():
            if op.get_file_obj() in self._stopped_serving and fut and not fut.done():
                fut.cancel()

    def _handle_cqe(self, cqe: IoUringCqe):
        try:
            op, fut = self._cache.pop(cqe.user_data)
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

        if fut:
            op.mark_seen(cqe.user_data)
            # TODO: figure out the correct way to _stopped_serving, may be io_uring_prep_cancel_fd?
            if op.get_file_obj() in self._stopped_serving:
                # the self.cancel_operation woulbe be triggered
                # if the user_data is seen, the op.get_user_data would not appeared
                fut.cancel()
            else:
                self._run_operation(cqe, op, fut)
        # if fut is None, it means it from cancelation
        elif cqe.res < 0:
            if self._loop is None or cqe.res == -2 and op.all_seen():
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
        for _, fut in list(self._cache.values()):
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
