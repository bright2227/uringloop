import socket

import pytest

from uringloop.proactor import IoUringProactor  # Assuming this is your custom proactor implementation


@pytest.mark.asyncio
async def test_udp_communication(
    init_proactor: IoUringProactor, client_udp_sock: socket.socket, server_udp_sock: socket.socket
) -> None:
    """Test UDP communication between client and server."""
    proactor = init_proactor
    server_address = ("127.0.0.1", server_udp_sock.getsockname()[1])

    test_messages = [
        b"Hello, World!",
        b"X" * 1024 + b"Large message",  # Test with larger data
        b"\x00\xff\xaa\xbb",  # Test with binary data
    ]

    for message in test_messages:
        # Send data from client to server
        sent_bytes = await proactor.sendto(client_udp_sock, message, addr=server_address)
        assert sent_bytes == len(message), f"Sent {sent_bytes} bytes but message was {len(message)} bytes"

        # Receive data on server side
        received_data, client_addr = await proactor.recvfrom(server_udp_sock, 4096)
        assert received_data == message, f"Received {received_data} but sent {message}"

        # Send response back from server to client
        sent_bytes = await proactor.sendto(server_udp_sock, received_data, addr=client_addr)
        assert sent_bytes == len(message), f"Sent {sent_bytes} bytes but message was {len(message)} bytes"

        # Receive response on client side
        response_data, _ = await proactor.recvfrom(client_udp_sock, 4096)
        assert response_data == message, f"Received {response_data} but expected {message}"

    # Test recvfrom_into version
    for message in test_messages:
        # Send data from client to server
        await proactor.sendto(client_udp_sock, message, addr=server_address)

        # Receive using recvfrom_into on server side
        buf = bytearray(4096)
        nbytes, client_addr = await proactor.recvfrom_into(server_udp_sock, buf)
        assert buf[:nbytes] == message, f"Received {buf[:nbytes]} but sent {message}"

        # Send response back
        await proactor.sendto(server_udp_sock, buf[:nbytes], addr=client_addr)

        # Receive using recvfrom_into on client side
        buf = bytearray(4096)
        nbytes, _ = await proactor.recvfrom_into(client_udp_sock, buf)
        assert buf[:nbytes] == message, f"Received {buf[:nbytes]} but expected {message}"
