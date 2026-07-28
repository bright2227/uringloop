from abc import ABC, abstractmethod
from collections.abc import Buffer
from dataclasses import dataclass, field
from io import BufferedReader, IOBase
import os
import socket
from typing import TYPE_CHECKING, Annotated, Any

from uringloop._types import IoUringCqe, Sockaddr
from uringloop.lib import parse_addr


if TYPE_CHECKING:
    from uringloop.proactor import _IoUringFuture  # type: ignore[reportPrivateUsage]


@dataclass(slots=True)
class BaseOperation(ABC):
    @abstractmethod
    def get_user_data(
        self,
    ) -> int | None:
        # for IoUringProactor.cancel_operation
        ...

    @abstractmethod
    def get_file_obj(self) -> Any:
        ...
        # for IoUringProactor._stop_serving

    @abstractmethod
    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"): ...

    @abstractmethod
    def mark_seen(self, user_data: int) -> None: ...

    @abstractmethod
    def all_seen(self) -> bool: ...


def get_os_error(res: int) -> OSError:
    return OSError(-res, os.strerror(-res))


@dataclass(slots=True)
class SingleCqeOperation(BaseOperation):
    """An operation completed by exactly one CQE.

    Subclasses only define their fields, get_file_obj and _result.
    """

    user_data: int = field(kw_only=True)
    cqe_received: bool = field(default=False, kw_only=True)

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def mark_seen(self, user_data: int) -> None:
        if user_data != self.user_data:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")
        self.cqe_received = True

    def all_seen(self) -> bool:
        return self.cqe_received

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result(self._result(res))

    @abstractmethod
    def _result(self, res: int) -> Any:
        """Build the future's result from a non-negative CQE res."""
        ...


@dataclass(slots=True)
class SendOperation(SingleCqeOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "readable"]
    flags: int

    def get_file_obj(self) -> Any:
        return self.sock

    def _result(self, res: int) -> int:
        return res


@dataclass(slots=True)
class WriteOperation(SingleCqeOperation):
    file: IOBase
    buffer: Annotated[Buffer, "readable"]
    offset: int

    def get_file_obj(self) -> Any:
        return self.file

    def _result(self, res: int) -> int:
        return res


@dataclass(slots=True)
class RecvOperation(SingleCqeOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    flags: int

    def get_file_obj(self) -> Any:
        return self.sock

    def _result(self, res: int) -> bytes:
        return memoryview(self.buffer)[:res].tobytes()


@dataclass(slots=True)
class ReadOperation(SingleCqeOperation):
    file: IOBase
    buffer: Annotated[Buffer, "writable"]

    def get_file_obj(self) -> Any:
        return self.file

    def _result(self, res: int) -> bytes:
        return memoryview(self.buffer)[:res].tobytes()


@dataclass(slots=True)
class RecvIntoOperation(SingleCqeOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    flags: int

    def get_file_obj(self) -> Any:
        return self.sock

    def _result(self, res: int) -> int:
        return res


@dataclass(slots=True)
class ReadIntoOperation(SingleCqeOperation):
    file: IOBase
    buffer: Annotated[Buffer, "writable"]

    def get_file_obj(self) -> Any:
        return self.file

    def _result(self, res: int) -> int:
        return res


@dataclass(slots=True)
class RecvFromOperation(SingleCqeOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    sockaddr: Annotated[Sockaddr, "writable"]
    flags: int

    def get_file_obj(self) -> Any:
        return self.sock

    def _result(self, res: int) -> tuple[bytes, Any]:
        return (memoryview(self.buffer)[:res].tobytes(), parse_addr(self.sock.family, self.sockaddr))


@dataclass(slots=True)
class RecvFromIntoOperation(SingleCqeOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    sockaddr: Annotated[Sockaddr, "writable"]
    flags: int

    def get_file_obj(self) -> Any:
        return self.sock

    def _result(self, res: int) -> tuple[int, Any]:
        return (res, parse_addr(self.sock.family, self.sockaddr))


@dataclass(slots=True)
class SendToOperation(SingleCqeOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "readable"]
    sockaddr: Annotated[Sockaddr, "readable"] | None
    flags: int

    def get_file_obj(self) -> Any:
        return self.sock

    def _result(self, res: int) -> int:
        return res


@dataclass(slots=True)
class ConnectOperation(SingleCqeOperation):
    sock: socket.socket
    sockaddr: Annotated[Sockaddr, "readable"]

    def get_file_obj(self) -> Any:
        return self.sock

    def _result(self, res: int) -> None:
        return None


@dataclass(slots=True)
class AcceptOperation(SingleCqeOperation):
    sock: socket.socket
    sockaddr: Annotated[Sockaddr, "writable"]
    flags: int

    def get_file_obj(self) -> Any:
        return self.sock

    def _result(self, res: int) -> tuple[socket.socket, Any]:
        # socket.fromfd would dup() the fd and leak the original one;
        # wrap the accepted fd directly instead (same as socket.accept)
        sock = socket.socket(self.sock.family, self.sock.type, self.sock.proto, fileno=res)
        if socket.getdefaulttimeout() is None and self.sock.gettimeout():
            sock.setblocking(True)
        return (sock, parse_addr(self.sock.family, self.sockaddr))


@dataclass(slots=True)
class PollAddOperation(SingleCqeOperation):
    file: IOBase | socket.socket | int  # some source is not a obj
    poll_mask: int

    def get_file_obj(self) -> Any:
        return self.file

    def _result(self, res: int) -> int:
        return res


@dataclass(slots=True)
class SendfileOperation(BaseOperation):
    sock: socket.socket
    file: BufferedReader
    pipe_r: int
    pipe_w: int
    offset: int
    count: int
    # splice  from file to pipe
    f2p_user_data: int
    # splice  from pipe to socket
    p2s_user_data: int
    f2p_done: bool = False
    p2s_done: bool = False
    pipes_closed: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        """Seems two cqes are linked, you only need to remove the first unfinished one."""
        if not self.f2p_done:
            return self.f2p_user_data
        elif not self.p2s_done:
            return self.p2s_user_data

    def get_file_obj(self) -> Any:
        return self.sock

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        elif self.p2s_done:
            fut.set_result(res)

    def mark_seen(self, user_data: int):
        if user_data == self.f2p_user_data:
            self.f2p_done = True
        elif user_data == self.p2s_user_data:
            self.p2s_done = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")
        # both linked CQEs have arrived (success, failure or cancellation), so
        # the kernel no longer references the pipe; release the fds
        if self.f2p_done and self.p2s_done and not self.pipes_closed:
            self.pipes_closed = True
            os.close(self.pipe_r)
            os.close(self.pipe_w)

    def all_seen(self) -> bool:
        return self.p2s_done
