from asyncio import base_events, events, futures, proactor_events, tasks, unix_events
from collections.abc import Buffer
import errno
import io
import os
import socket
import stat
from typing import Any, Callable, cast
import warnings

from uringloop.lib import POLLERR, POLLHUP, POLLIN
from uringloop.log import logger
from uringloop.proactor import IoUringProactor


class _IouringWritePipeTransport(proactor_events._ProactorBaseWritePipeTransport):  # type: ignore[reportPrivateUsage]
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._read_fut = cast(IoUringProactor, self._loop._proactor).poll_add(self._sock, POLLERR | POLLHUP)  # type: ignore[reportPrivateUsage]
        self._read_fut.add_done_callback(self._pipe_closed)

    def _pipe_closed(self, fut: futures.Future[int]):
        if fut.cancelled():
            # the transport has been closed
            return
        poll_mask = fut.result()
        assert (poll_mask | POLLHUP) or (poll_mask | POLLERR)
        if self._closing:
            assert self._read_fut is None
            return
        assert fut is self._read_fut, (fut, self._read_fut)
        self._read_fut = None
        if self._write_fut is not None:
            self._force_close(BrokenPipeError())
        else:
            self.close()


class IouringProactorEventLoop(proactor_events.BaseProactorEventLoop):
    """Linux version of proactor event loop using Iouring."""

    def __init__(self, proactor: IoUringProactor | None = None):
        if proactor is None:
            proactor = IoUringProactor()
        super().__init__(proactor)
        self._proactor: IoUringProactor

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

    async def sock_sendall(self, sock: socket.socket | io.IOBase, data: Buffer):
        total_length: int = len(memoryview(data))
        while total_length > 0:
            sent = await self._proactor.send(sock, data)
            total_length -= sent
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
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            watcher = events.get_child_watcher()

        with watcher:
            if not watcher.is_active():
                # Check early.
                # Raising exception before process creation
                # prevents subprocess execution if the watcher
                # is not ready to handle it.
                raise RuntimeError("asyncio.get_child_watcher() is not activated, subprocess support is not installed.")
            waiter = self.create_future()
            transp = unix_events._UnixSubprocessTransport(   # type: ignore[reportPrivateUsage]
                self, protocol, args, shell, stdin, stdout, stderr, bufsize, waiter=waiter, extra=extra, **kwargs
            )
            watcher.add_child_handler(cast(int, transp.get_pid()), self._child_watcher_callback, transp)
            try:
                await waiter
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException:
                transp.close()
                await transp._wait()
                raise

        return transp

    def _make_write_pipe_transport(self, sock, protocol, waiter=None, extra=None):
        return _IouringWritePipeTransport(self, sock, protocol, waiter, extra)

    def _child_watcher_callback(self, pid: int, returncode: int, transp: unix_events._UnixSubprocessTransport):   # type: ignore[reportPrivateUsage]
        self.call_soon_threadsafe(transp._process_exited, returncode)


class UnixProactorPidfdChildWatcher(unix_events.AbstractChildWatcher):
    """Child watcher implementation using io uring and Linux's pid file descriptors."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        pass

    def is_active(self):
        return True

    def close(self):
        pass

    def attach_loop(self, loop: events.AbstractEventLoop | None):
        pass

    def add_child_handler(self, pid: int, callback: Callable[[int, int, *tuple[Any, ...]], Any], *args: tuple[Any, ...]):
        loop = events.get_running_loop()
        pidfd = os.pidfd_open(pid)
        fut: futures.Future[int] = cast(IoUringProactor, loop._proactor).poll_add(pidfd, POLLIN)   # type: ignore[reportPrivateUsage]

        def _do_wait(fut: futures.Future[int]):
            try:
                _, status = os.waitpid(pid, 0)
            except ChildProcessError:
                returncode = 255
                logger.warning("child process pid %d exit status already read:  will report returncode 255", pid)
            else:
                returncode = unix_events.waitstatus_to_exitcode(status)

            os.close(pidfd)
            callback(pid, returncode, *args)

        fut.add_done_callback(_do_wait)

    def remove_child_handler(self, pid: int):
        # asyncio never calls remove_child_handler() !!!
        # The method is no-op but is implemented because
        # abstract base classes require it.
        return True


class IouringProactorEventLoopPolicy(events.BaseDefaultEventLoopPolicy):
    _loop_factory = IouringProactorEventLoop

    def __init__(self):
        super().__init__()
        self._watcher: unix_events.AbstractChildWatcher | None = None

    def _init_watcher(self):
        with events._lock:
            if self._watcher is None:
                self._watcher = UnixProactorPidfdChildWatcher()

    def get_child_watcher(self):
        """Get the watcher for child processes.

        If not yet set, a ThreadedChildWatcher object is automatically created.
        """
        if self._watcher is None:
            self._init_watcher()

        warnings._deprecated(
            "get_child_watcher",
            "{name!r} is deprecated as of Python 3.12 and will be removed in Python {remove}.",
            remove=(3, 14),
        )
        return self._watcher

    def set_child_watcher(self, watcher: unix_events.AbstractChildWatcher | None):
        """Set the watcher for child processes."""

        assert watcher is None or isinstance(watcher, unix_events.AbstractChildWatcher)

        if self._watcher is not None:
            self._watcher.close()

        self._watcher = watcher
        warnings._deprecated(
            "set_child_watcher",
            "{name!r} is deprecated as of Python 3.12 and will be removed in Python {remove}.",
            remove=(3, 14),
        )
