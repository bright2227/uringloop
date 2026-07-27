from collections.abc import Buffer
from dataclasses import dataclass, field
from io import IOBase
import socket
from typing import Annotated, Any, TypeAlias

from uringloop._types import Sockaddr


@dataclass(slots=True)
class SendRequest:
    sock: socket.socket
    buffer: Annotated[Buffer, "readable"]
    flags: int
    _buffer_cdata: Any = field(default=None, init=False, repr=False)


@dataclass(slots=True)
class WriteRequest:
    file: IOBase
    buffer: Annotated[Buffer, "readable"]
    offset: int
    _buffer_cdata: Any = field(default=None, init=False, repr=False)


@dataclass(slots=True)
class RecvRequest:
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    flags: int
    _buffer_cdata: Any = field(default=None, init=False, repr=False)


@dataclass(slots=True)
class ReadRequest:
    file: IOBase
    buffer: Annotated[Buffer, "writable"]
    offset: int
    _buffer_cdata: Any = field(default=None, init=False, repr=False)


@dataclass(slots=True)
class AcceptRequest:
    sock: socket.socket
    sockaddr: Annotated[Sockaddr, "writable"]
    flags: int
    _addrlen_ptr: Any = field(default=None, init=False, repr=False)


@dataclass(slots=True)
class ConnectRequest:
    sock: socket.socket
    sockaddr: Annotated[Sockaddr, "readable"]


@dataclass(slots=True)
class Cancel64Request:
    user_data: int
    flags: int


@dataclass(slots=True)
class SendToRequest:
    sock: socket.socket
    buffer: Annotated[Buffer, "readable"]
    sockaddr: Annotated[Sockaddr, "readable"] | None
    msghdr_flags: int
    flags: int
    _msghdr: Any = field(default=None, init=False, repr=False)
    _iov: Any = field(default=None, init=False, repr=False)
    _iov_base: Any = field(default=None, init=False, repr=False)


@dataclass(slots=True)
class RecvFromRequest:
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    sockaddr: Annotated[Sockaddr, "writable"]
    msghdr_flags: int
    flags: int
    _msghdr: Any = field(default=None, init=False, repr=False)
    _iov: Any = field(default=None, init=False, repr=False)
    _iov_base: Any = field(default=None, init=False, repr=False)


@dataclass(slots=True)
class SpliceRequest:
    file_in: IOBase | socket.socket | int
    off_in: int
    file_out: IOBase | socket.socket | int
    off_out: int
    nbytes: int
    splice_flags: int


@dataclass(slots=True)
class PollAddRequest:
    file: IOBase | socket.socket | int
    poll_mask: int


KernelRequest: TypeAlias = (
    SendRequest
    | WriteRequest
    | RecvRequest
    | ReadRequest
    | AcceptRequest
    | ConnectRequest
    | Cancel64Request
    | SendToRequest
    | RecvFromRequest
    | SpliceRequest
    | PollAddRequest
)
