from asyncio import base_events, events, futures, proactor_events, tasks, unix_events
from collections import deque
from collections.abc import Buffer
import errno
import io
import os
import socket
import stat
import threading
from typing import Any, cast

from uringloop import _uringcore
from uringloop.lib import POLLERR, POLLHUP, POLLIN
from uringloop.log import logger
from uringloop.proactor import IoUringProactor


class _NativeReadyQueue:
    """Deque-compatible adapter backed by the native ready queue.

    ``BaseEventLoop._run_once`` first reads the queue length and then calls
    ``popleft`` that many times. The first pop snapshots the native queue into
    one Python batch, so callbacks scheduled while that batch runs remain in
    the native core until the next tick.
    """

    __slots__ = ("_batch", "_core")

    def __init__(self, core=None):
        self._core = _uringcore.LoopCore() if core is None else core
        self._batch = deque()

    def __len__(self):
        return len(self._batch) + self._core.ready_count

    def append(self, handle):
        self._core.enqueue_ready(handle)

    def popleft(self):
        if not self._batch:
            ready_count = self._core.ready_count
            if ready_count == 0:
                raise IndexError("pop from an empty deque")
            self._batch.extend(self._core.take_ready_batch(ready_count))
        return self._batch.popleft()

    def clear(self):
        self._batch.clear()
        self._core.clear_ready()

    def close(self):
        self._batch.clear()
        self._core.close()


class _IoUringWritePipeTransport(proactor_events._ProactorBaseWritePipeTransport):  # type: ignore[reportPrivateUsage]
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._read_fut = cast(IoUringProactor, self._loop._proactor).poll_add(self._sock, POLLERR | POLLHUP)  # type: ignore[reportPrivateUsage]
        self._read_fut.add_done_callback(self._pipe_closed)

    def _pipe_closed(self, fut: futures.Future[int]):
        if fut.cancelled():
            # the transport has been closed
            return
        poll_mask = fut.result()
        assert poll_mask & (POLLHUP | POLLERR)
        if self._closing:
            assert self._read_fut is None
            return
        assert fut is self._read_fut, (fut, self._read_fut)
        self._read_fut = None
        if self._write_fut is not None:
            self._force_close(BrokenPipeError())
        else:
            self.close()


class IoUringProactorEventLoop(proactor_events.BaseProactorEventLoop):
    """Linux version of proactor event loop using IoUring."""

    def __init__(
        self,
        proactor: IoUringProactor | None = None,
        *,
        ready_queue: str = "python",
        timer_heap: str = "python",
    ):
        # BaseEventLoop.__del__ may run when constructing the default proactor
        # fails before the base initializer has set this field.
        self._closed = True
        if ready_queue not in {"python", "native"}:
            raise ValueError("ready_queue must be 'python' or 'native'")
        if timer_heap not in {"python", "native"}:
            raise ValueError("timer_heap must be 'python' or 'native'")
        if timer_heap == "native" and ready_queue != "native":
            raise ValueError("the native timer heap requires ready_queue='native'")
        if proactor is None:
            proactor = IoUringProactor()
        super().__init__(proactor)
        self._proactor: IoUringProactor
        self._ready_queue_mode = ready_queue
        self._timer_heap_mode = timer_heap
        self._timer_core = None
        if ready_queue == "native":
            if self._ready:
                raise RuntimeError("cannot enable the native ready queue after scheduling callbacks")
            core = _uringcore.LoopCore()
            self._ready = _NativeReadyQueue(core)
            if timer_heap == "native":
                self._timer_core = core

    def call_at(self, when, callback, *args, context=None):
        if self._timer_core is None:
            return super().call_at(when, callback, *args, context=context)
        if when is None:
            raise TypeError("when cannot be None")
        self._check_closed()
        if self._debug:
            self._check_thread()
            self._check_callback(callback, "call_at")
        timer = events.TimerHandle(when, callback, args, self, context)
        if timer._source_traceback:
            del timer._source_traceback[-1]
        self._timer_core.schedule_timer(timer, when)
        timer._scheduled = True
        return timer

    def _run_once(self):
        if self._timer_core is None:
            return super()._run_once()

        core = self._timer_core
        scheduled_count = core.timer_count
        if (
            scheduled_count > base_events._MIN_SCHEDULED_TIMER_HANDLES
            and self._timer_cancelled_count / scheduled_count > base_events._MIN_CANCELLED_TIMER_HANDLES_FRACTION
        ):
            core.compact_timers()
            self._timer_cancelled_count = 0

        deadline = core.next_deadline()
        if self._timer_cancelled_count:
            self._timer_cancelled_count = core.cancelled_timer_count
        timeout = None
        if self._ready or self._stopping:
            timeout = 0
        elif deadline is not None:
            timeout = min(max(0, deadline - self.time()), base_events.MAXIMUM_SELECT_TIMEOUT)

        event_list = self._selector.select(timeout)
        self._process_events(event_list)
        event_list = None

        core.promote_due(self.time(), self._clock_resolution, 4096)
        if self._timer_cancelled_count:
            self._timer_cancelled_count = core.cancelled_timer_count

        ready_count = len(self._ready)
        for _ in range(ready_count):
            handle = self._ready.popleft()
            if handle._cancelled:
                continue
            if self._debug:
                try:
                    self._current_handle = handle
                    started = self.time()
                    handle._run()
                    elapsed = self.time() - started
                    if elapsed >= self.slow_callback_duration:
                        logger.warning(
                            "Executing %s took %.3f seconds",
                            base_events._format_handle(handle),
                            elapsed,
                        )
                finally:
                    self._current_handle = None
            else:
                handle._run()
        handle = None

    def close(self):
        ready = self._ready
        super().close()
        if isinstance(ready, _NativeReadyQueue):
            ready.close()

    def _run_forever_setup(self):
        assert self._self_reading_future is None
        self.call_soon(self._loop_self_reading)
        super()._run_forever_setup()

    def _run_forever_cleanup(self):
        super()._run_forever_cleanup()
        if self._self_reading_future is not None:
            self._self_reading_future.cancel()
            # TODO make sure https://github.com/python/cpython/blob/d16f455cd8cabbc1e7bd2369cdb8718c30ab8957/Lib/asyncio/windows_events.py#L328-L333
            self._self_reading_future = None

    def run_forever(self):
        # Python 3.14+ calls the setup/cleanup hooks above. Python 3.12 and
        # 3.13 inline the run loop and therefore need the proactor self-read
        # lifecycle here so call_soon_threadsafe can wake an idle ring wait.
        if hasattr(super(), "_run_forever_setup"):
            return super().run_forever()

        assert self._self_reading_future is None
        self.call_soon(self._loop_self_reading)
        try:
            return super().run_forever()
        finally:
            if self._self_reading_future is not None:
                self._self_reading_future.cancel()
                self._self_reading_future = None

    async def sock_sendall(self, sock: socket.socket | io.IOBase, data: Buffer):
        view = memoryview(data).cast("B")
        while len(view) > 0:
            sent = await self._proactor.send(sock, view)
            # a partial send must continue after the sent bytes, not resend
            # the whole buffer from the start
            view = view[sent:]
        return

    async def create_unix_connection(
        self,
        protocol_factory,
        path=None,
        *,
        ssl=None,
        sock=None,
        server_hostname=None,
        ssl_handshake_timeout=None,
        ssl_shutdown_timeout=None,
    ):
        assert server_hostname is None or isinstance(server_hostname, str)
        if ssl:
            if server_hostname is None:
                raise ValueError("you have to pass server_hostname when using ssl")
        else:
            if server_hostname is not None:
                raise ValueError("server_hostname is only meaningful with ssl")
            if ssl_handshake_timeout is not None:
                raise ValueError("ssl_handshake_timeout is only meaningful with ssl")
            if ssl_shutdown_timeout is not None:
                raise ValueError("ssl_shutdown_timeout is only meaningful with ssl")

        if path is not None:
            if sock is not None:
                raise ValueError("path and sock can not be specified at the same time")

            path = os.fspath(path)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
            try:
                sock.setblocking(False)
                await self.sock_connect(sock, path)
            except:
                sock.close()
                raise

        else:
            if sock is None:
                raise ValueError("no path and sock were specified")
            if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
                raise ValueError(f"A UNIX Domain Stream Socket was expected, got {sock!r}")
            sock.setblocking(False)

        transport, protocol = await self._create_connection_transport(
            sock,
            protocol_factory,
            ssl,
            server_hostname,
            ssl_handshake_timeout=ssl_handshake_timeout,
            ssl_shutdown_timeout=ssl_shutdown_timeout,
        )
        return transport, protocol

    async def create_unix_server(
        self,
        protocol_factory,
        path=None,
        *,
        sock=None,
        backlog=100,
        ssl=None,
        ssl_handshake_timeout=None,
        ssl_shutdown_timeout=None,
        start_serving: bool = True,
    ):
        if isinstance(ssl, bool):
            raise TypeError("ssl argument must be an SSLContext or None")

        if ssl_handshake_timeout is not None and not ssl:
            raise ValueError("ssl_handshake_timeout is only meaningful with ssl")

        if ssl_shutdown_timeout is not None and not ssl:
            raise ValueError("ssl_shutdown_timeout is only meaningful with ssl")

        if path is not None:
            if sock is not None:
                raise ValueError("path and sock can not be specified at the same time")

            path = os.fspath(path)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

            # Check for abstract socket. `str` and `bytes` paths are supported.
            if path[0] not in (0, "\x00"):
                try:
                    if stat.S_ISSOCK(os.stat(path).st_mode):
                        os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError as err:
                    # Directory may have permissions only to create socket.
                    logger.error("Unable to check or remove stale UNIX socket %r: %r", path, err)

            try:
                sock.bind(path)
            except OSError as exc:
                sock.close()
                if exc.errno == errno.EADDRINUSE:
                    # Let's improve the error message by adding
                    # with what exact address it occurs.
                    msg = f"Address {path!r} is already in use"
                    raise OSError(errno.EADDRINUSE, msg) from None
                else:
                    raise
            except:
                sock.close()
                raise
        else:
            if sock is None:
                raise ValueError("path was not specified, and no sock specified")

            if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
                raise ValueError(f"A UNIX Domain Stream Socket was expected, got {sock!r}")

        sock.setblocking(False)
        server = base_events.Server(self, [sock], protocol_factory, ssl, backlog, ssl_handshake_timeout, ssl_shutdown_timeout)
        if start_serving:
            server._start_serving()
            await tasks.sleep(0)

        return server

    async def _make_subprocess_transport(
        self, protocol, args, shell, stdin, stdout, stderr, bufsize: int, extra=None, **kwargs
    ):
        waiter = self.create_future()
        transp = unix_events._UnixSubprocessTransport(  # type: ignore[reportPrivateUsage]
            self, protocol, args, shell, stdin, stdout, stderr, bufsize, waiter=waiter, extra=extra, **kwargs
        )
        self._watch_child(cast(int, transp.get_pid()), transp)
        try:
            await waiter
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException:
            transp.close()
            await transp._wait()
            raise

        return transp

    def _watch_child(self, pid: int, transp: "unix_events._UnixSubprocessTransport"):  # type: ignore[reportPrivateUsage]
        """Reap the child through a pidfd polled by io_uring.

        This replaces the child watcher API, which was removed in Python 3.14.
        Some portable CPython builds omit ``os.pidfd_open`` even on a new
        enough Linux kernel, so retain a waitpid thread as a compatibility
        fallback for those interpreters.
        """
        pidfd_open = getattr(os, "pidfd_open", None)
        if pidfd_open is None:
            self._start_waitpid_thread(pid, transp)
            return

        pidfd = None
        try:
            pidfd = pidfd_open(pid)
            fut: futures.Future[int] = self._proactor.poll_add(pidfd, POLLIN)
        except (OSError, RuntimeError):
            if pidfd is not None:
                try:
                    os.close(pidfd)
                except OSError:
                    pass
            self._start_waitpid_thread(pid, transp)
            return

        def _child_exited(fut: futures.Future[int]):
            try:
                if fut.cancelled():
                    # the poll was cancelled (e.g. loop shutdown) so the child
                    # may still be running; waitpid would block the event loop
                    return
                try:
                    _, status = os.waitpid(pid, 0)
                except ChildProcessError:
                    returncode = 255
                    logger.warning("child process pid %d exit status already read:  will report returncode 255", pid)
                else:
                    returncode = os.waitstatus_to_exitcode(status)
            finally:
                os.close(pidfd)

            self.call_soon(transp._process_exited, returncode)  # type: ignore[reportPrivateUsage]

        fut.add_done_callback(_child_exited)

    def _start_waitpid_thread(self, pid: int, transp: "unix_events._UnixSubprocessTransport"):  # type: ignore[reportPrivateUsage]
        thread = threading.Thread(
            target=self._waitpid_in_thread,
            args=(pid, transp),
            name=f"uringloop-waitpid-{pid}",
            daemon=True,
        )
        thread.start()

    def _waitpid_in_thread(self, pid: int, transp: "unix_events._UnixSubprocessTransport"):  # type: ignore[reportPrivateUsage]
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                returncode = os.waitstatus_to_exitcode(status)
                break
            except InterruptedError:
                continue
            except ChildProcessError:
                returncode = 255
                logger.warning("child process pid %d exit status already read: will report returncode 255", pid)
                break

        try:
            self.call_soon_threadsafe(transp._process_exited, returncode)  # type: ignore[reportPrivateUsage]
        except RuntimeError:
            # The loop may have been closed while its child was still alive.
            pass

    def _make_write_pipe_transport(self, sock, protocol, waiter=None, extra=None):
        return _IoUringWritePipeTransport(self, sock, protocol, waiter, extra)


# Preserve the child-watcher methods on Python 3.12 and 3.13 by using the Unix
# policy while it exists. Python 3.14 removed that policy and renamed the
# generic base, so fall back to whichever generic name is available there.
_BaseDefaultEventLoopPolicy: type[Any] = getattr(unix_events, "_UnixDefaultEventLoopPolicy", None) or (
    getattr(events, "BaseDefaultEventLoopPolicy", None) or getattr(events, "_BaseDefaultEventLoopPolicy")
)


class IoUringProactorEventLoopPolicy(_BaseDefaultEventLoopPolicy):
    _loop_factory = IoUringProactorEventLoop


# deprecated aliases kept for backwards compatibility with the 0.1.x naming
IouringProactorEventLoop = IoUringProactorEventLoop
IouringProactorEventLoopPolicy = IoUringProactorEventLoopPolicy
