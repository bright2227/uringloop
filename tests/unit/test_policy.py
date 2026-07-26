import asyncio.unix_events
import warnings

import pytest

from uringloop.loop import IouringProactorEventLoopPolicy


@pytest.mark.skipif(
    not hasattr(asyncio.unix_events, "AbstractChildWatcher"),
    reason="the child-watcher API was removed in Python 3.14",
)
def test_policy_preserves_child_watcher_api():
    policy = IouringProactorEventLoopPolicy()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        watcher = policy.get_child_watcher()

    assert isinstance(watcher, asyncio.unix_events.AbstractChildWatcher)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        policy.set_child_watcher(None)
