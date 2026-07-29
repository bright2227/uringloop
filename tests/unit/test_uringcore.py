import pytest

from uringloop import _uringcore


def test_native_core_has_versioned_abi():
    assert _uringcore.ABI_VERSION == 1


def test_ring_rejects_empty_queue():
    with pytest.raises(ValueError, match="entries must be greater than zero"):
        _uringcore.Ring(0)


def test_ring_owns_and_releases_kernel_resources():
    ring = _uringcore.Ring(8)

    assert ring.sq_entries >= 8
    assert ring.cq_entries >= ring.sq_entries
    assert ring.closed is False

    ring.close()
    assert ring.closed is True

    ring.close()
    assert ring.closed is True


def test_ring_context_manager_closes_resources():
    with _uringcore.Ring(entries=8) as ring:
        assert ring.closed is False

    assert ring.closed is True
    with pytest.raises(RuntimeError, match="ring is closed"):
        ring.__enter__()
