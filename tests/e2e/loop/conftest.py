import asyncio
import os
import tempfile
from typing import cast

import pytest
import pytest_asyncio

from uringloop.loop import IouringProactorEventLoop, IouringProactorEventLoopPolicy


@pytest_asyncio.fixture(scope="package", autouse=True)
async def event_loop_policy():
    asyncio.set_event_loop_policy(IouringProactorEventLoopPolicy())

# TODO: remove pytest_asyncio warning
@pytest.fixture
def event_loop():
    loop = asyncio.get_event_loop()
    yield cast(IouringProactorEventLoop, loop)
    loop.close()

@pytest.fixture
def unix_socket_path():
    with tempfile.TemporaryDirectory() as f:
        yield os.path.join(f, "test.sock")
