from abc import ABC, abstractmethod
from collections.abc import Buffer
from dataclasses import dataclass
from io import BufferedReader, IOBase
import os
import socket
from typing import TYPE_CHECKING, Annotated, Any

from uringloop.lib import IoUringCqe, Sockaddr, parse_addr


if TYPE_CHECKING:
    from proactor import _IoUringFuture  # type: ignore[reportPrivateUsage]


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
class SendOperation(BaseOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "readable"]
    flags: int
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.sock

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result(res)

    def mark_seen(self, user_data: int):
        if user_data == self.user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


@dataclass(slots=True)
class WriteOperation(BaseOperation):
    file: IOBase
    buffer: Annotated[Buffer, "readable"]
    offset: int
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.file

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result(res)

    def mark_seen(self, user_data: int):
        if user_data == self.user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


@dataclass(slots=True)
class RecvOperation(BaseOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    flags: int
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.sock

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result(memoryview(self.buffer)[:res].tobytes())

    def mark_seen(self, user_data: int):
        if self.user_data == user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


@dataclass(slots=True)
class ReadOperation(BaseOperation):
    file: IOBase
    buffer: Annotated[Buffer, "writable"]
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.file

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result(memoryview(self.buffer)[:res].tobytes())

    def mark_seen(self, user_data: int):
        if self.user_data == user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


@dataclass(slots=True)
class RecvIntoOperation(BaseOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    flags: int
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.sock

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result(res)

    def mark_seen(self, user_data: int):
        if self.user_data == user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


@dataclass(slots=True)
class ReadIntoOperation(BaseOperation):
    file: IOBase
    buffer: Annotated[Buffer, "writable"]
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.file

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result(res)

    def mark_seen(self, user_data: int):
        if self.user_data == user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


@dataclass(slots=True)
class RecvFromOperation(BaseOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    sockaddr: Annotated[Sockaddr, "writable"]
    flags: int
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.sock

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result((memoryview(self.buffer)[:res].tobytes(), parse_addr(self.sock.family, self.sockaddr)))

    def mark_seen(self, user_data: int):
        if self.user_data == user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


@dataclass(slots=True)
class RecvFromIntoOperation(BaseOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    sockaddr: Annotated[Sockaddr, "writable"]
    flags: int
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.sock

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result((res, parse_addr(self.sock.family, self.sockaddr)))

    def mark_seen(self, user_data: int):
        if self.user_data == user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


@dataclass(slots=True)
class SendToOperation(BaseOperation):
    sock: socket.socket
    buffer: Annotated[Buffer, "readable"]
    sockaddr: Annotated[Sockaddr, "readable"] | None
    flags: int
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.sock

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result(res)

    def mark_seen(self, user_data: int):
        if self.user_data == user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


@dataclass(slots=True)
class ConnectOperation(BaseOperation):
    sock: socket.socket
    sockaddr: Annotated[Sockaddr, "readable"]
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.sock

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result(None)

    def mark_seen(self, user_data: int):
        if self.user_data == user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


@dataclass(slots=True)
class AcceptOperation(BaseOperation):
    sock: socket.socket
    sockaddr: Annotated[Sockaddr, "writable"]
    flags: int
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.sock

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            # TODO make sure new socket flags inheritation
            sock = socket.fromfd(res, self.sock.family, self.sock.type, self.sock.proto)
            if socket.getdefaulttimeout() is None and self.sock.gettimeout():
                sock.setblocking(True)
            fut.set_result((sock, parse_addr(self.sock.family, self.sockaddr)))

    def mark_seen(self, user_data: int):
        if self.user_data == user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received


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

    def all_seen(self) -> bool:
        return self.p2s_done


@dataclass(slots=True)
class PollAddOperation(BaseOperation):
    file: IOBase | socket.socket | int  # some source is not a obj
    poll_mask: int
    user_data: int
    cqe_received: bool = False

    def get_user_data(
        self,
    ) -> int | None:
        return None if self.cqe_received else self.user_data

    def get_file_obj(self) -> Any:
        return self.file

    def operate(self, cqe: IoUringCqe, fut: "_IoUringFuture"):
        res: int = cqe.res
        if res < 0:
            fut.set_exception(get_os_error(res))
        else:
            fut.set_result(res)

    def mark_seen(self, user_data: int):
        if self.user_data == user_data:
            self.cqe_received = True
        else:
            raise RuntimeError(f"Unknown user_data: {user_data} is not expected.")

    def all_seen(self) -> bool:
        return self.cqe_received
