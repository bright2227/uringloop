import asyncio
from typing import cast

import pytest

from uringloop.loop import IouringProactorEventLoop, IouringProactorEventLoopPolicy


@pytest.fixture(scope="session", autouse=True)
def event_loop_policy():
    asyncio.set_event_loop_policy(IouringProactorEventLoopPolicy())

@pytest.fixture
def event_loop():
    loop = asyncio.get_event_loop()
    yield cast(IouringProactorEventLoop, loop)
    loop.close()
