import pytest

from uringloop import _uringcore_liburing


def test_static_liburing_core_has_versioned_abi():
    assert _uringcore_liburing.ABI_VERSION == 1


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
