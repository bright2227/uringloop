from collections.abc import Buffer
import os
import socket
from typing import Annotated, Any

from uringloop._liburing import ffi, lib
from uringloop._types import (
    IoUring,
    IoUringCqe,
    IoUringSqe,
    Iovec,
    KernelTimespec,
    Sockaddr,
    SockaddrIn,
    SockaddrIn6,
    SockaddrUn,
    SocklenT,
    pyAddress,
)
from uringloop.request import (
    AcceptRequest,
    Cancel64Request,
    ConnectRequest,
    PollAddRequest,
    ReadRequest,
    RecvFromRequest,
    RecvRequest,
    SendRequest,
    SendToRequest,
    SpliceRequest,
    WriteRequest,
)


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


# -1 as an unsigned __u64: read/write at the file's current position
# (kernel 5.16+); pipes and sockets ignore the offset either way
OFFSET_CURRENT_POS: int = 0xFFFF_FFFF_FFFF_FFFF


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


def _export_buffer(buffer: Buffer, *, require_writable: bool = False) -> tuple[Any, int]:
    cdata = ffi.from_buffer(buffer, require_writable=require_writable)
    return cdata, ffi.sizeof(cdata)


def new_iovec(
    buf: Annotated[Buffer, "readable/writable"], *, require_writable: bool = False
) -> tuple[Annotated[Iovec, "readable/writable"], Any]:
    """
    Create and initialize a struct iovec.

    Args:
        buf (Buffer)
        require_writable (bool): Reject read-only buffers when the kernel will
            write into the iovec.

    Returns:
        A tuple (iovec, iov_base): assigning into iov.iov_base does not keep the
        from_buffer cdata alive, so it is returned alongside and must be kept
        referenced as long as the iovec is in use.
    """
    iov = ffi.new("struct iovec *")
    iov_base, nbytes = _export_buffer(buf, require_writable=require_writable)
    iov.iov_base = iov_base  # type: ignore[reportAttributeAccessIssue]
    iov.iov_len = nbytes  # type: ignore[reportAttributeAccessIssue]
    return iov, iov_base


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
    res = lib.io_uring_queue_init(entries, ring, flags)
    if res < 0:
        raise OSError(-res, os.strerror(-res))
    return res


def io_uring_queue_exit(ring: IoUring) -> int:
    return lib.io_uring_queue_exit(ring)


def io_uring_get_sqe(ring: IoUring) -> IoUringSqe | None:
    """Returns None when the submission queue is full."""
    sqe = lib.io_uring_get_sqe(ring)
    if sqe == ffi.NULL:
        return None
    return sqe


def io_uring_prep_send(sqe: IoUringSqe, request: SendRequest) -> None:
    buf, nbytes = _export_buffer(request.buffer)
    request._buffer_cdata = buf
    lib.io_uring_prep_send(sqe, request.sock.fileno(), buf, nbytes, request.flags)


def io_uring_prep_recv(sqe: IoUringSqe, request: RecvRequest) -> None:
    buf, nbytes = _export_buffer(request.buffer, require_writable=True)
    request._buffer_cdata = buf
    lib.io_uring_prep_recv(sqe, request.sock.fileno(), buf, nbytes, request.flags)


def io_uring_prep_write(sqe: IoUringSqe, request: WriteRequest) -> None:
    buf, nbytes = _export_buffer(request.buffer)
    request._buffer_cdata = buf
    lib.io_uring_prep_write(sqe, request.file.fileno(), buf, nbytes, request.offset)


def io_uring_prep_read(sqe: IoUringSqe, request: ReadRequest) -> None:
    buf, nbytes = _export_buffer(request.buffer, require_writable=True)
    request._buffer_cdata = buf
    lib.io_uring_prep_read(sqe, request.file.fileno(), buf, nbytes, request.offset)


def io_uring_prep_accept(sqe: IoUringSqe, request: AcceptRequest) -> None:
    # the kernel writes the peer address length into *addrlen_ptr at completion
    # time, so it must stay alive until the CQE is consumed
    addrlen_ptr = ffi.new("socklen_t *")
    addrlen_ptr[0] = get_sockaddr_size(request.sock.family)
    request._addrlen_ptr = addrlen_ptr

    lib.io_uring_prep_accept(
        sqe,
        request.sock.fileno(),
        ffi.cast("struct sockaddr *", request.sockaddr),
        addrlen_ptr,
        request.flags,
    )


def io_uring_prep_connect(sqe: IoUringSqe, request: ConnectRequest) -> None:
    addrlen = new_socklen_t(request.sock.family)
    lib.io_uring_prep_connect(
        sqe,
        request.sock.fileno(),
        ffi.cast("struct sockaddr *", request.sockaddr),
        addrlen,
    )


def io_uring_prep_poll_add(sqe: IoUringSqe, request: PollAddRequest) -> None:
    fd = request.file if isinstance(request.file, int) else request.file.fileno()
    lib.io_uring_prep_poll_add(sqe, fd, request.poll_mask)


def io_uring_prep_cancel64(sqe: IoUringSqe, request: Cancel64Request) -> None:
    lib.io_uring_prep_cancel64(sqe, request.user_data, request.flags)


#  TODO: update as  io_uring_prep_sendmsg
def io_uring_prep_sendto(sqe: IoUringSqe, request: SendToRequest) -> None:
    iov, iov_base = new_iovec(buf=request.buffer)
    msghdr = ffi.new("struct msghdr *")

    msghdr.msg_iov = iov  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_iovlen = 1  # type: ignore[reportAttributeAccessIssue]

    msghdr.msg_control = ffi.NULL  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_controllen = 0  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_flags = request.msghdr_flags  # type: ignore[reportAttributeAccessIssue]

    if request.sockaddr is None:
        msghdr.msg_name = ffi.NULL  # type: ignore[reportAttributeAccessIssue]
        msghdr.msg_namelen = 0  # type: ignore[reportAttributeAccessIssue]
    else:
        msghdr.msg_name = ffi.cast("struct sockaddr *", request.sockaddr)  # type: ignore[reportAttributeAccessIssue]
        msghdr.msg_namelen = get_sockaddr_size(request.sock.family)  # type: ignore[reportAttributeAccessIssue]

    request._msghdr = msghdr
    request._iov = iov
    request._iov_base = iov_base
    lib.io_uring_prep_sendmsg(sqe, request.sock.fileno(), msghdr, request.flags)


#  TODO: update as  io_uring_prep_recvfrom
def io_uring_prep_recvfrom(sqe: IoUringSqe, request: RecvFromRequest) -> None:
    iov, iov_base = new_iovec(buf=request.buffer, require_writable=True)
    msghdr = ffi.new("struct msghdr *")

    msghdr.msg_iov = iov  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_iovlen = 1  # type: ignore[reportAttributeAccessIssue]

    msghdr.msg_control = ffi.NULL  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_controllen = 0  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_flags = request.msghdr_flags  # type: ignore[reportAttributeAccessIssue]

    msghdr.msg_name = request.sockaddr  # type: ignore[reportAttributeAccessIssue]
    msghdr.msg_namelen = get_sockaddr_size(request.sock.family)  # type: ignore[reportAttributeAccessIssue]

    request._msghdr = msghdr
    request._iov = iov
    request._iov_base = iov_base
    lib.io_uring_prep_recvmsg(sqe, request.sock.fileno(), msghdr, request.flags)


def io_uring_prep_splice(sqe: IoUringSqe, request: SpliceRequest) -> None:
    fd_in = request.file_in if isinstance(request.file_in, int) else request.file_in.fileno()
    fd_out = request.file_out if isinstance(request.file_out, int) else request.file_out.fileno()

    lib.io_uring_prep_splice(
        sqe,
        fd_in,
        request.off_in,
        fd_out,
        request.off_out,
        request.nbytes,
        request.splice_flags,
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
