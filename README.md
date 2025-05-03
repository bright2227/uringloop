# uringloop

A Python implementation of a liburing-based proactor event loop for asyncio, designed to follow Python standard library conventions. The implementation is written primarily in pure Python, using CFFI only for wrapping the liburing interface.

- Implements a Proactor pattern similar to Windows' IOCP ([reference implementation](https://github.com/python/cpython/blob/d16f455cd8cabbc1e7bd2369cdb8718c30ab8957/Lib/asyncio/windows_events.py#L417))
- Follows Python's standard asyncio implementation patterns

> ⚠️ Note: This is currently experimental and not yet stable

## Goals

- Provide a primarily Python (with CFFI, maybe C extention in future) implementation of a liburing-based event loop
- Maintain full compatibility with standard asyncio APIs
- Follow Python standard library implementation patterns

## Requirements

- Linux 5.19+ kernel

- Python 3.12+

- liburing development libraries

  ```bash
  sudo apt install liburing-dev
  ```

## Installation

- Using uv:

```bash
uv add uringloop
```

- Using pip:

```bash
pip install uringloop
```

## Quick Start

Set the event loop policy with asyncio.set_event_loop_policy(IouringProactorEventLoopPolicy()) to use the io_uring-based event loop.

```python
import asyncio

from uringloop import IouringProactorEventLoopPolicy


asyncio.set_event_loop_policy(IouringProactorEventLoopPolicy())


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info('peername')
    print(f"Connection from {addr}")

    while True:
        data = await reader.read(1024)
        if not data:
            break
        print(f"Received: {data.decode()}")
        writer.write(data)
        await writer.drain()

    print(f"Connection with {addr} closed")
    writer.close()


async def start_server():
    server = await asyncio.start_server(
        handle_client, '127.0.0.1', 8888)
    addr = server.sockets[0].getsockname()
    print(f'Serving on {addr}')

    async with server:
        await server.serve_forever()


async def tcp_client(message: str):
    reader, writer = await asyncio.open_connection(
        '127.0.0.1', 8888)

    print(f'Send: {message!r}')
    writer.write(message.encode())
    await writer.drain()

    data = await reader.read(100)
    print(f'Received: {data.decode()!r}')

    print('Closing connection')
    writer.close()
    await writer.wait_closed()


async def main():
    # Start the server in the background
    server_task = asyncio.create_task(start_server())

    # Give the server a moment to start
    await asyncio.sleep(1)

    # Run the client
    await tcp_client('Hello, World!')

    # Cancel the server task (otherwise it runs forever)
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())

```

the result would looks like

```console
Serving on ('127.0.0.1', 8888)
Send: 'Hello, World!'
Connection from ('0.0.0.0', 0)
Received: Hello, World!
Received: 'Hello, World!'
Closing connection
Connection with ('0.0.0.0', 0) closed
```

## Contributing

Contributions are welcome! Please open issues or pull requests for any bugs or feature requests.

## License

This project is licensed under the terms of the MIT license.

## TODO

- use registered fd and buffer
- add signal handling
- refactor e2e test
- benchmark
- figure out a way to do unit/integration test
