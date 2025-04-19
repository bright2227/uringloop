import io
import os
import socket

import pytest

from uringloop.proactor import IoUringProactor


@pytest.mark.asyncio
async def test_tcp_conmmunication(
    init_proactor: IoUringProactor, client_tcp_sock: socket.socket, server_tcp_sock: socket.socket
) -> None:
    """Integration test using real socket communication with the echo server."""
    _, port = server_tcp_sock.getsockname()
    proactor = init_proactor
    address = ("127.0.0.1", port)

    await proactor.connect(client_tcp_sock, address)
    incoming_client_tcp_socket, client_tcp_sock_address = await proactor.accept(server_tcp_sock)

    assert client_tcp_sock.getsockname() == client_tcp_sock_address

    test_messages = [
        b"Hello, World!",
        b"X" * 1024 + b"Large message",  # Test with larger data
        b"\x00\xff\xaa\xbb",  # Test with binary data
    ]

    for message in test_messages:
        # Send data using proactor
        sent_bytes = await proactor.send(incoming_client_tcp_socket, message)

        # Verify the number of bytes sent
        assert sent_bytes == len(message), f"Sent {sent_bytes} bytes but message was {len(message)} bytes"

        # Read the echoed response
        received_data = await proactor.recv(client_tcp_sock, 4096)

        # Verify the echoed data matches what we sent
        assert received_data == message, f"Received {received_data} but sent {message}"

    for message in test_messages:
        # Send data using proactor
        sent_bytes = await proactor.send(incoming_client_tcp_socket, message)

        # Verify the number of bytes sent
        assert sent_bytes == len(message), f"Sent {sent_bytes} bytes but message was {len(message)} bytes"

        # Read the echoed response into a buffer
        buf = bytearray(4096)
        received_data_count = await proactor.recv_into(client_tcp_sock, buf)

        # Verify the echoed data matches what we sent
        assert received_data_count == len(message), f"Received {received_data_count} bytes but expected {len(message)}"
        assert buf[:received_data_count] == message, f"Received {buf[:received_data_count]} but sent {message}"


@pytest.mark.asyncio
async def test_sendfile_operation(
    init_proactor: IoUringProactor, client_tcp_sock: socket.socket, server_tcp_sock: socket.socket, test_file: io.BufferedReader
) -> None:
    """Test sendfile operation between client and server."""
    _, port = server_tcp_sock.getsockname()
    proactor = init_proactor
    address = ("127.0.0.1", port)

    await proactor.connect(client_tcp_sock, address)
    incoming_client_tcp_socket, _ = await proactor.accept(server_tcp_sock)

    # Get file size
    test_file.seek(0, os.SEEK_END)
    file_size = test_file.tell()
    test_file.seek(0)

    # Send file using sendfile
    sent_bytes = await proactor.sendfile(incoming_client_tcp_socket, test_file, 0, file_size)
    assert sent_bytes == file_size, f"Sent {sent_bytes} bytes but file was {file_size} bytes"

    # Receive the file content on client side
    received_data = await proactor.recv(client_tcp_sock, sent_bytes)
    test_file.seek(0)
    assert received_data == test_file.read(-1), "Received file content doesn't match original"

    # Verify the content matches
    offset = int(file_size // 2)
    count = file_size
    test_file.seek(0)
    sent_bytes = await proactor.sendfile(incoming_client_tcp_socket, test_file, offset, file_size)
    assert (file_size - offset) == sent_bytes, "Received file content doesn't match original"

    # Receive the partial content
    received_data = await proactor.recv(client_tcp_sock, count)
    test_file.seek(offset)
    expected_data = test_file.read(count)
    assert received_data == expected_data, "Received partial file content doesn't match expected"
