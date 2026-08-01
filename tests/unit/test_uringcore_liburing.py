import errno
import time

import pytest

from uringloop import _uringcore_liburing


def test_static_liburing_core_has_versioned_abi():
    assert _uringcore_liburing.ABI_VERSION == 2


def reap_requests(ring, count):
    completed = []
    deadline = time.monotonic() + 1
    while len(completed) < count and time.monotonic() < deadline:
        completed.extend(ring.reap(count - len(completed)))
        if len(completed) < count:
            time.sleep(0.001)
    assert len(completed) == count
    return completed


@pytest.mark.parametrize("entries", [-(2**32), -1, 0, 2**32, 2**64])
def test_static_liburing_ring_rejects_out_of_range_queue_size(entries):
    with pytest.raises(ValueError, match="entries must be between"):
        _uringcore_liburing.Ring(entries)


def test_static_liburing_ring_owns_and_releases_kernel_resources():
    ring = _uringcore_liburing.Ring(8)

    assert ring.sq_entries >= 8
    assert ring.cq_entries >= ring.sq_entries
    assert ring.closed is False

    ring.close()
    assert ring.closed is True

    ring.close()
    assert ring.closed is True


def test_static_liburing_ring_context_manager_closes_resources():
    with _uringcore_liburing.Ring(entries=8) as ring:
        assert ring.closed is False

    assert ring.closed is True
    with pytest.raises(RuntimeError, match="ring is closed"):
        ring.__enter__()


def test_static_liburing_ring_submits_and_reaps_native_nop_requests():
    ring = _uringcore_liburing.Ring(8)
    requests = [ring.prepare_nop(), ring.prepare_nop()]

    assert ring.pending == 2
    assert [request.state for request in requests] == ["prepared", "prepared"]
    assert [request.result for request in requests] == [None, None]
    assert ring.submit() == 2
    assert [request.state for request in requests] == ["submitted", "submitted"]

    completed = reap_requests(ring, 2)

    assert set(completed) == set(requests)
    assert ring.pending == 0
    assert [request.state for request in requests] == ["completed", "completed"]
    assert [request.done for request in requests] == [True, True]
    assert [request.cancelled for request in requests] == [False, False]
    assert [request.result for request in requests] == [0, 0]
    assert [request.flags for request in requests] == [0, 0]


def test_static_liburing_ring_keeps_request_alive_until_completion():
    ring = _uringcore_liburing.Ring(8)

    ring.prepare_nop()
    assert ring.submit() == 1

    [request] = reap_requests(ring, 1)
    assert request.state == "completed"
    assert request.result == 0


def test_static_liburing_ring_close_releases_prepared_request_ownership():
    ring = _uringcore_liburing.Ring(8)
    request = ring.prepare_nop()

    ring.close()

    assert ring.pending == 0
    assert request.state == "cancelled"
    assert request.done is True
    assert request.cancelled is True
    assert request.result == -errno.ECANCELED


@pytest.mark.parametrize("max_completions", [-1, 0, 2**32])
def test_static_liburing_ring_reap_rejects_out_of_range_batch_size(
    max_completions,
):
    ring = _uringcore_liburing.Ring(8)

    with pytest.raises(ValueError, match="max_completions must be between"):
        ring.reap(max_completions)
