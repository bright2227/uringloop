from collections.abc import Buffer
from io import IOBase
import os
import socket
from typing import Annotated, Any, TypeAlias, TypedDict, Unpack

from uringloop._liburing import ffi, lib


IoUring = Any
IoUringSqe = Any
IoUringCqe = Any
KernelTimespec = Any
Iovec = Any
Msghdr = Any
SockaddrIn = Any
SockaddrIn6 = Any
SockaddrUn = Any
SocklenT = Any
Sockaddr = SockaddrIn | SockaddrIn6 | SockaddrUn
pyAddress: TypeAlias = tuple[Any, ...] | str | Buffer


IOSQE_FIXED_FILE: int = lib.IOSQE_FIXED_FILE
IOSQE_IO_DRAIN: int = lib.IOSQE_IO_DRAIN
IOSQE_IO_LINK: int = lib.IOSQE_IO_LINK
IOSQE_IO_HARDLINK: int = lib.IOSQE_IO_HARDLINK
IOSQE_ASYNC: int = lib.IOSQE_ASYNC
IOSQE_BUFFER_SELECT: int = lib.IOSQE_BUFFER_SELECT
IOSQE_CQE_SKIP_SUCCESS: int = lib.IOSQE_CQE_SKIP_SUCCESS


IORING_SETUP_IOPOLL: int = lib.IORING_SETUP_IOPOLL
IORING_SETUP_SQPOLL: int = lib.IORING_SETUP_SQPOLL
IORING_SETUP_SQ_AFF: int = lib.IORING_SETUP_SQ_AFF
IORING_SETUP_CQSIZE: int = lib.IORING_SETUP_CQSIZE
IORING_SETUP_CLAMP: int = lib.IORING_SETUP_CLAMP
IORING_SETUP_ATTACH_WQ: int = lib.IORING_SETUP_ATTACH_WQ
IORING_SETUP_R_DISABLED: int = lib.IORING_SETUP_R_DISABLED
IORING_SETUP_SUBMIT_ALL: int = lib.IORING_SETUP_SUBMIT_ALL
IORING_SETUP_COOP_TASKRUN: int = lib.IORING_SETUP_COOP_TASKRUN
IORING_SETUP_TASKRUN_FLAG: int = lib.IORING_SETUP_TASKRUN_FLAG
IORING_SETUP_SQE128: int = lib.IORING_SETUP_SQE128
IORING_SETUP_CQE32: int = lib.IORING_SETUP_CQE32
IORING_SETUP_SINGLE_ISSUER: int = lib.IORING_SETUP_SINGLE_ISSUER
IORING_SETUP_DEFER_TASKRUN: int = lib.IORING_SETUP_DEFER_TASKRUN
IORING_SETUP_NO_MMAP: int = lib.IORING_SETUP_NO_MMAP
IORING_SETUP_REGISTERED_FD_ONLY: int = lib.IORING_SETUP_REGISTERED_FD_ONLY


POLLIN: int = lib.POLLIN
POLLPRI: int = lib.POLLPRI
POLLOUT: int = lib.POLLOUT
POLLERR: int = lib.POLLERR
POLLHUP: int = lib.POLLHUP
POLLNVAL: int = lib.POLLNVAL


class SendKwargs(TypedDict):
    sock: socket.socket
    buffer: Annotated[Buffer, "readable"]
    flags: int


class WriteKwargs(TypedDict):
    file: IOBase
    buffer: Annotated[Buffer, "readable"]
    offset: int


class RecvKwargs(TypedDict):
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    flags: int


class ReadKwargs(TypedDict):
    file: IOBase
    buffer: Annotated[Buffer, "writable"]
    offset: int


class AcceptKwargs(TypedDict):
    sock: socket.socket
    sockaddr: Annotated[Sockaddr, "writable"]
    flags: int


class ConnectKwargs(TypedDict):
    sock: socket.socket
    sockaddr: Annotated[Sockaddr, "readable"]


class Cancel64Kwargs(TypedDict):
    user_data: int
    flags: int


class SendToKwargs(TypedDict):
    sock: socket.socket
    buffer: Annotated[Buffer, "readable"]
    sockaddr: Annotated[Sockaddr, "readable"] | None  # for tcp, you don't need to get address
    msghdr_flags: int
    flags: int


class RecvFromKwargs(TypedDict):
    sock: socket.socket
    buffer: Annotated[Buffer, "writable"]
    sockaddr: Annotated[Sockaddr, "writable"]
    msghdr_flags: int
    flags: int


class SpliceKwargs(TypedDict):
    file_in: IOBase | socket.socket | int
    off_in: int
    file_out: IOBase | socket.socket | int
    off_out: int
    nbytes: int
    splice_flags: int


class PollAddKwargs(TypedDict):
    file: IOBase | socket.socket | int
    poll_mask: int


# Structure creation functions
def new_io_uring() -> IoUring:
    return ffi.new("struct io_uring *")


def new_sockaddr_ipv4() -> SockaddrIn:
    """
    Create a sockaddr_in structure for IPv4.
    Returns a pointer to struct sockaddr.
    """
    sockaddr = ffi.new("struct sockaddr_in *")
    sockaddr.sin_family = socket.AF_INET  # type: ignore[reportAttributeAccessIssue]
    return sockaddr


def set_sockaddr_ipv4(sockaddr: SockaddrIn, ip: str, port: int):
    sockaddr.sin_port = socket.htons(port)  # Convert port to network byte order
    sockaddr.sin_addr.s_addr = int.from_bytes(socket.inet_aton(ip), "little")  # Convert IP to 4-byte binary


def new_sockaddr_ipv6() -> SockaddrIn6:
    """
    Create a sockaddr_in6 structure for IPv6.
    Returns a pointer to struct sockaddr.
    """
    sockaddr = ffi.new("struct sockaddr_in6 *")
    sockaddr.sin6_family = socket.AF_INET6  # type: ignore[reportAttributeAccessIssue]
    return sockaddr


def set_sockaddr_ipv6(sockaddr: SockaddrIn6, ip: str, port: int, flowinfo: int = 0, scope_id: int = 0):
    sockaddr.sin6_port = socket.htons(port)  # Convert port to network byte order
    packed_ip = socket.inet_pton(socket.AF_INET6, ip)  # Convert IP to 16-byte binary
    # Copy the packed IP address to s6_addr byte by byte
    for i in range(len(packed_ip)):
        sockaddr.sin6_addr.s6_addr[i] = packed_ip[i]
    sockaddr.sin6_flowinfo = flowinfo
    sockaddr.sin6_scope_id = scope_id


def new_sockaddr_unix() -> SockaddrUn:
    """
    Create a sockaddr_un structure for Unix domain sockets.
    Returns a pointer to struct sockaddr.
    """
    sockaddr = ffi.new("struct sockaddr_un *")
    sockaddr.sun_family = socket.AF_UNIX  # type: ignore[reportAttributeAccessIssue]
    return sockaddr


def set_sockaddr_unix(sockaddr: SockaddrUn, path: str | Buffer):
    if isinstance(path, str):
        path_bytes = path.encode()
        length = len(path_bytes)
    else:
        path_bytes = path
        length = len(memoryview(path_bytes))

    ffi.memmove(sockaddr.sun_path, path_bytes, length)  # Copy the path into sun_path


def parse_ipv4_addr(sockaddr: SockaddrIn) -> tuple[str, int]:
    """
    Parse an IPv4 address from the addr_buffer.
    Returns a tuple of (ip: str, port: int).
    """
    # Use socket.inet_ntop to convert the packed IP to string
    ip = socket.inet_ntop(socket.AF_INET, ffi.buffer(ffi.addressof(sockaddr.sin_addr), 4))  # type: ignore[reportAttributeAccessIssue]

    # Extract port: convert from network byte order
    port = socket.ntohs(sockaddr.sin_port)
    return (ip, port)


def parse_ipv6_addr(sockaddr: SockaddrIn6) -> tuple[str, int, int, int]:
    """
    Parse an IPv6 address from the addr_buffer.
    Returns a tuple of (ip: str, port: int, flowinfo: int, scope_id: int).
    """
    # Use socket.inet_ntop to convert the packed IPv6 address to string
    ip = socket.inet_ntop(socket.AF_INET6, ffi.buffer(ffi.addressof(sockaddr.sin6_addr), 16))  # type: ignore[reportAttributeAccessIssue]

    # Extract port: convert from network byte order
    port = socket.ntohs(sockaddr.sin6_port)

    # Extract flowinfo and scope_id (these are already in host byte order)
    flowinfo = sockaddr.sin6_flowinfo
    scope_id = sockaddr.sin6_scope_id

    return (ip, port, flowinfo, scope_id)


def parse_unix_addr(sockaddr: SockaddrUn) -> bytes:
    """
    Parse a Unix domain socket address from the addr_buffer.
    Return a path: bytes.
    """
    return ffi.string(sockaddr.sun_path)  # type: ignore[reportAttributeAccessIssue]


def parse_addr(family: socket.AddressFamily, sockaddr: SockaddrUn) -> pyAddress:
    match family:
        case socket.AF_INET:
            return parse_ipv4_addr(sockaddr)
        case socket.AF_INET6:
            return parse_ipv6_addr(sockaddr)
        case socket.AF_UNIX:
            return parse_unix_addr(sockaddr)
        case _:
            raise ValueError("Unsupported address family")


def new_kernel_timespec(tv_sec: int = 0, tv_nsec: int = 0) -> KernelTimespec:
    """
    Create and initialize a __kernel_timespec structure.

    Args:
        tv_sec (int): Seconds (default: 0).
        tv_nsec (int): Nanoseconds (default: 0).

    Returns:
        A pointer to the initialized struct __kernel_timespec.
    """
    ts = ffi.new("struct __kernel_timespec *")
    ts.tv_sec = tv_sec  # type: ignore[reportAttributeAccessIssue]
    ts.tv_nsec = tv_nsec  # type: ignore[reportAttributeAccessIssue]
    return ts


def new_iovec(buf: Annotated[Buffer, "writable/immwritable"]) -> Annotated[Iovec, "writable/immwritable"]:
    """
    Create and initialize a struct iovec.

    Args:
        buf (Buffer)

    Returns:
        A pointer to the initialized struct iovec.
    """
    iov = ffi.new("struct iovec *")
    iov.iov_base = ffi.from_buffer(buf)  # type: ignore[reportAttributeAccessIssue]
    iov.iov_len = len(buf)  # type: ignore[reportAttributeAccessIssue]
    return iov


def get_sockaddr_size(family: socket.AddressFamily) -> int:
    match family:
        case socket.AF_INET:
            return ffi.sizeof("struct sockaddr_in")
        case socket.AF_INET6:
            return ffi.sizeof("struct sockaddr_in6")
        case socket.AF_UNIX:
            return ffi.sizeof("struct sockaddr_un")
        case _:
            raise ValueError("Unsupported address family")


def new_socklen_t(family: socket.AddressFamily) -> SocklenT:
    return ffi.cast("socklen_t", get_sockaddr_size(family))


def new_writable_sockaddr(family: socket.AddressFamily) -> Annotated[Sockaddr, "writable"]:
    match family:
        case socket.AF_INET:
            return new_sockaddr_ipv4()
        case socket.AF_INET6:
            return new_sockaddr_ipv6()
        case socket.AF_UNIX:
            return new_sockaddr_unix()
        case _:
            raise ValueError("Unsupported address family")


def new_readable_sockaddr(family: socket.AddressFamily, address: pyAddress) -> Annotated[Sockaddr, "readable"]:
    match family:
        case socket.AF_INET:
            sockaddr = new_sockaddr_ipv4()
            assert isinstance(address, tuple)
            set_sockaddr_ipv4(sockaddr, *address)
            return sockaddr
        case socket.AF_INET6:
            sockaddr = new_sockaddr_ipv6()
            assert isinstance(address, tuple)
            set_sockaddr_ipv6(sockaddr, *address)
            return sockaddr
        case socket.AF_UNIX:
            sockaddr = new_sockaddr_unix()
            assert isinstance(address, (str, Buffer))
            set_sockaddr_unix(sockaddr, address)
            return sockaddr
        case _:
            raise ValueError("Unsupported address family")


# Function wrappers with type annotations
def io_uring_queue_init(entries: int, ring: IoUring, flags: int = 0) -> int:
    return lib.io_uring_queue_init(entries, ring, flags)


def io_uring_queue_exit(ring: IoUring) -> int:
    return lib.io_uring_queue_exit(ring)


def io_uring_get_sqe(ring: IoUring) -> IoUringSqe:
    return lib.io_uring_get_sqe(ring)


def io_uring_prep_send(sqe: IoUringSqe, **kwargs: Unpack[SendKwargs]) -> None:
    sock = kwargs["sock"]
    buffer = kwargs["buffer"]
    flags = kwargs["flags"]

    lib.io_uring_prep_send(sqe, sock.fileno(), ffi.from_buffer(buffer), len(buffer), flags)  # type: ignore[reportAttributeAccessIssue]


def io_uring_prep_recv(sqe: IoUringSqe, **kwargs: Unpack[RecvKwargs]) -> None:
    sock = kwargs["sock"]
    buffer = kwargs["buffer"]
    flags = kwargs["flags"]

    lib.io_uring_prep_recv(sqe, sock.fileno(), ffi.from_buffer(buffer), len(buffer), flags)  # type: ignore[reportAttributeAccessIssue]


def io_uring_prep_write(sqe: IoUringSqe, **kwargs: Unpack[WriteKwargs]) -> None:
    file = kwargs["file"]
    buffer = kwargs["buffer"]
    offset = kwargs["offset"]

    lib.io_uring_prep_write(sqe, file.fileno(), ffi.from_buffer(buffer), len(buffer), offset)  # type: ignore[reportAttributeAccessIssue]


def io_uring_prep_read(sqe: IoUringSqe, **kwargs: Unpack[ReadKwargs]) -> None:
    file = kwargs["file"]
    buffer = kwargs["buffer"]
    offset = kwargs["offset"]

    lib.io_uring_prep_read(sqe, file.fileno(), ffi.from_buffer(buffer), len(buffer), offset)  # type: ignore[reportAttributeAccessIssue]


def io_uring_prep_accept(sqe: IoUringSqe, **kwargs: Unpack[AcceptKwargs]) -> None:
    sock = kwargs["sock"]
    sockaddr = kwargs["sockaddr"]
    flags = kwargs["flags"]
    addrlen = new_socklen_t(sock.family)
    addrlen_ptr = ffi.new("socklen_t *addrlen")
    addrlen_ptr[0] = addrlen

    lib.io_uring_prep_accept(sqe, sock.fileno(), ffi.cast("struct sockaddr *", sockaddr), addrlen_ptr, flags)


def io_uring_prep_connect(sqe: IoUringSqe, **kwargs: Unpack[ConnectKwargs]) -> None:
    sock = kwargs["sock"]
    sockaddr = kwargs["sockaddr"]
    addrlen = new_socklen_t(sock.family)

    lib.io_uring_prep_connect(sqe, sock.fileno(), ffi.cast("struct sockaddr *", sockaddr), addrlen)


def io_uring_prep_poll_add(sqe: IoUringSqe, **kwargs: Unpack[PollAddKwargs]) -> None:
    file = kwargs["file"]
    poll_mask = kwargs["poll_mask"]
    fd = file if isinstance(file, int) else file.fileno()
    lib.io_uring_prep_poll_add(sqe, fd, poll_mask)


def io_uring_prep_cancel64(sqe: IoUringSqe, **kwargs: Unpack[Cancel64Kwargs]) -> None:
    user_data = kwargs["user_data"]
    flags = kwargs["flags"]
    lib.io_uring_prep_cancel64(sqe, user_data, flags)


#  TODO: update as  io_uring_prep_sendmsg
def io_uring_prep_sendto(sqe: IoUringSqe, **kwargs: Unpack[SendToKwargs]) -> None:
    sock = kwargs["sock"]
    buffer = kwargs["buffer"]
    flags = kwargs["flags"]
    sockaddr = kwargs["sockaddr"]
    msghdr_flags = kwargs["msghdr_flags"]

    iov = new_iovec(buf=buffer)
    msghdr = ffi.new("struct msghdr *")

    msghdr.msg_iov = iov  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_iovlen = 1  # type: ignore[reportAttributeAccessIssue]

    msghdr.msg_control = ffi.NULL  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_controllen = 0  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_flags = msghdr_flags  # type: ignore[reportAttributeAccessIssue]

    if sockaddr is None:
        msghdr.msg_name = ffi.NULL  # type: ignore[reportAttributeAccessIssue]
        msghdr.msg_namelen = 0  # type: ignore[reportAttributeAccessIssue]
    else:
        msghdr.msg_name = ffi.cast("struct sockaddr *", sockaddr)  # type: ignore[reportAttributeAccessIssue]
        msghdr.msg_namelen = get_sockaddr_size(sock.family)  # type: ignore[reportAttributeAccessIssue]

    lib.io_uring_prep_sendmsg(sqe, sock.fileno(), msghdr, flags)


#  TODO: update as  io_uring_prep_recvfrom
def io_uring_prep_recvfrom(sqe: IoUringSqe, **kwargs: Unpack[RecvFromKwargs]) -> None:
    sock = kwargs["sock"]
    buf = kwargs["buffer"]
    sockaddr = kwargs["sockaddr"]
    msghdr_flags = kwargs["msghdr_flags"]
    flags = kwargs["flags"]

    iov = new_iovec(buf=buf)
    msghdr = ffi.new("struct msghdr *")

    msghdr.msg_iov = iov  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_iovlen = 1  # type: ignore[reportAttributeAccessIssue]

    msghdr.msg_control = ffi.NULL  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_controllen = 0  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_flags = msghdr_flags  # type: ignore[reportAttributeAccessIssue]

    msghdr.msg_name = sockaddr  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_namelen = get_sockaddr_size(sock.family)  # type: ignore[reportAttributeAccessIssue]

    lib.io_uring_prep_recvmsg(sqe, sock.fileno(), msghdr, flags)


def io_uring_prep_splice(sqe: IoUringSqe, **kwargs: Unpack[SpliceKwargs]) -> None:
    file_in = kwargs["file_in"]
    off_in = kwargs["off_in"]
    file_out = kwargs["file_out"]
    off_out = kwargs["off_out"]
    nbytes = kwargs["nbytes"]
    splice_flags = kwargs["splice_flags"]
    fd_in = file_in if isinstance(file_in, int) else file_in.fileno()
    fd_out = file_out if isinstance(file_out, int) else file_out.fileno()

    lib.io_uring_prep_splice(
        sqe,
        fd_in,
        off_in,
        fd_out,
        off_out,
        nbytes,
        splice_flags,
    )


def io_uring_sqe_set_data64(sqe: IoUringSqe, data: int) -> None:
    lib.io_uring_sqe_set_data64(sqe, data)


def io_uring_sqe_set_flags(sqe: IoUringSqe, flags: int) -> None:
    lib.io_uring_sqe_set_flags(sqe, flags)


def io_uring_cqe_seen(ring: IoUring, cqe: IoUringCqe) -> None:
    lib.io_uring_cqe_seen(ring, cqe)


def io_uring_submit(ring: IoUring) -> None:
    res = lib.io_uring_submit(ring)
    if res < 0:
        raise OSError(-res, os.strerror(-res))


def io_uring_peek_cqe(ring: IoUring) -> IoUringCqe:
    cqe_ptr = ffi.new("struct io_uring_cqe **")
    res = lib.io_uring_peek_cqe(ring, cqe_ptr)
    if res < 0:
        raise OSError(-res, os.strerror(-res))
    return cqe_ptr[0]  # type: ignore[reportAttributeAccessIssue]


def io_uring_wait_cqe(ring: IoUring) -> IoUringCqe:
    cqe_ptr = ffi.new("struct io_uring_cqe **")
    res = lib.io_uring_wait_cqe(ring, cqe_ptr)
    if res < 0:
        raise OSError(-res, os.strerror(-res))
    return cqe_ptr[0]  # type: ignore[reportAttributeAccessIssue]


def io_uring_wait_cqe_timeout(ring: IoUring, ts: KernelTimespec) -> IoUringCqe:
    cqe_ptr = ffi.new("struct io_uring_cqe **")
    res = lib.io_uring_wait_cqe_timeout(ring, cqe_ptr, ts)
    if res < 0:
        raise OSError(-res, os.strerror(-res))
    return cqe_ptr[0]  # type: ignore[reportAttributeAccessIssue]
