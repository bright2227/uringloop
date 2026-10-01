import weakref

import pytest

from uringloop.loop import _NativeReadyQueue


def test_native_ready_adapter_preserves_tick_snapshot_order():
    ready = _NativeReadyQueue()
    first = object()
    second = object()
    nested = object()
    ready.append(first)
    ready.append(second)

    assert ready.popleft() is first
    ready.append(nested)

    assert ready.popleft() is second
    assert ready.popleft() is nested
    assert len(ready) == 0
    with pytest.raises(IndexError, match="pop from an empty deque"):
        ready.popleft()


def test_native_ready_adapter_clear_releases_batch_and_core_references():
    class Handle:
        pass

    ready = _NativeReadyQueue()
    first = Handle()
    second = Handle()
    references = [weakref.ref(first), weakref.ref(second)]
    ready.append(first)
    ready.append(second)
    assert ready.popleft() is first
    del first
    del second

    ready.clear()

    assert [reference() for reference in references] == [None, None]
    assert len(ready) == 0


def test_native_ready_adapter_close_is_idempotent():
    ready = _NativeReadyQueue()

    ready.close()
    ready.close()

    assert ready._core.closed is True
    with pytest.raises(RuntimeError, match="loop core is closed"):
        ready.append(object())
