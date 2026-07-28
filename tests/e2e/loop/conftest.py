import os
import tempfile

import pytest

from uringloop.loop import IoUringProactorEventLoopPolicy


@pytest.fixture(scope="package")
def event_loop_policy():
    # overriding this fixture is the documented pytest-asyncio way to run
    # every test in the package on loops created by this policy
    return IoUringProactorEventLoopPolicy()


@pytest.fixture
def unix_socket_path():
    with tempfile.TemporaryDirectory() as f:
        yield os.path.join(f, "test.sock")
