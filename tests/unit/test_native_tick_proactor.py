import pytest

from uringloop.native_proactor import NativeIoUringProactor


class FakeRing:
    cq_ready = 0
    cq_overflowed = False


def proactor_with_results(results):
    proactor = NativeIoUringProactor.__new__(NativeIoUringProactor)
    proactor._results = list(results)
    proactor._ring = FakeRing()
    proactor._pending_submit = False
    return proactor


def test_native_proactor_select_batch_retains_excess_resolved_completions():
    proactor = proactor_with_results([1, 2, 3, 4, 5])
    try:
        selected, immediate = proactor.select_batch(None, 2)
        assert selected == [1, 2]
        assert immediate is True
        assert proactor._results == [3, 4, 5]

        selected, immediate = proactor.select_batch(0, 3)
        assert selected == [3, 4, 5]
        assert immediate is False
    finally:
        proactor._ring = None


def test_native_proactor_select_batch_rejects_nonpositive_budget():
    proactor = proactor_with_results([1])
    try:
        with pytest.raises(ValueError, match="positive"):
            proactor.select_batch(0, 0)
    finally:
        proactor._ring = None
