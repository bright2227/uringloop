from array import array
import socket
import tempfile

import pytest

from uringloop._liburing import ffi
from uringloop.lib import (
    io_uring_prep_read,
    io_uring_prep_recv,
    io_uring_prep_recvfrom,
    io_uring_prep_send,
    io_uring_prep_write,
    new_iovec,
    new_writable_sockaddr,
)


def _new_sqe():
    return ffi.new("struct io_uring_sqe *")


def test_buffer_lengths_are_measured_in_bytes():
    buffer = array("I", [1, 2, 3, 4])
    expected_nbytes = memoryview(buffer).nbytes

    iov, _ = new_iovec(buffer)
    assert iov.iov_len == expected_nbytes

    sock, peer = socket.socketpair()
    try:
        send_sqe = _new_sqe()
        io_uring_prep_send(send_sqe, sock=sock, buffer=buffer, flags=0)
        assert send_sqe.len == expected_nbytes

        recv_sqe = _new_sqe()
        io_uring_prep_recv(recv_sqe, sock=sock, buffer=buffer, flags=0)
        assert recv_sqe.len == expected_nbytes
    finally:
        sock.close()
        peer.close()

    with tempfile.TemporaryFile() as file:
        write_sqe = _new_sqe()
        io_uring_prep_write(write_sqe, file=file, buffer=buffer, offset=0)
        assert write_sqe.len == expected_nbytes

        read_sqe = _new_sqe()
        io_uring_prep_read(read_sqe, file=file, buffer=buffer, offset=0)
        assert read_sqe.len == expected_nbytes


def test_receive_paths_reject_readonly_buffers():
    readonly_buffer = b"readonly"

    with pytest.raises(BufferError):
        new_iovec(readonly_buffer, require_writable=True)

    sock, peer = socket.socketpair()
    try:
        with pytest.raises(BufferError):
            io_uring_prep_recv(_new_sqe(), sock=sock, buffer=readonly_buffer, flags=0)
    finally:
        sock.close()
        peer.close()

    with tempfile.TemporaryFile() as file:
        with pytest.raises(BufferError):
            io_uring_prep_read(_new_sqe(), file=file, buffer=readonly_buffer, offset=0)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_sock:
        sockaddr = new_writable_sockaddr(socket.AF_INET)
        with pytest.raises(BufferError):
            io_uring_prep_recvfrom(
                _new_sqe(),
                sock=udp_sock,
                buffer=readonly_buffer,
                sockaddr=sockaddr,
                msghdr_flags=0,
                flags=0,
            )
