import errno
import time

import pytest

from uringloop import _uringcore


def test_native_core_has_versioned_abi():
    assert _uringcore.ABI_VERSION == 5
    assert _uringcore.Ring.__module__ == "uringloop._uringcore"
    assert _uringcore.Request.__module__ == "uringloop._uringcore"
    assert _uringcore.Completion.__module__ == "uringloop._uringcore"


def reap_completions(ring, count):
    completed = []
    deadline = time.monotonic() + 1
    while len(completed) < count and time.monotonic() < deadline:
        completed.extend(ring.reap(count - len(completed)))
        if len(completed) < count:
            time.sleep(0.001)
    assert len(completed) == count
    return completed


@pytest.mark.parametrize("entries", [-(2**32), -1, 0, 2**32, 2**64])
def test_native_ring_rejects_out_of_range_queue_size(entries):
    with pytest.raises(ValueError, match="entries must be between"):
        _uringcore.Ring(entries)


def test_native_ring_owns_and_releases_kernel_resources():
    ring = _uringcore.Ring(8)

    assert ring.sq_entries >= 8
    assert ring.cq_entries >= ring.sq_entries * 2
    assert ring.cq_ready == 0
    assert ring.cq_overflowed is False
    assert ring.cq_dropped == 0
    assert ring.closed is False

    ring.close()
    assert ring.closed is True
    assert ring.cq_ready == 0
    assert ring.cq_overflowed is False

    ring.close()
    assert ring.closed is True


def test_native_ring_context_manager_closes_resources():
    with _uringcore.Ring(entries=8) as ring:
        assert ring.closed is False

    assert ring.closed is True
    with pytest.raises(RuntimeError, match="ring is closed"):
        ring.__enter__()


def test_native_ring_submits_and_reaps_native_nop_requests():
    ring = _uringcore.Ring(8)
    requests = [ring.prepare_nop(), ring.prepare_nop()]

    assert ring.pending == 2
    assert [request.state for request in requests] == ["prepared", "prepared"]
    assert [request.result for request in requests] == [None, None]
    assert ring.submit() == 2
    assert [request.state for request in requests] == ["submitted", "submitted"]

    completions = reap_completions(ring, 2)

    assert {completion.request for completion in completions} == set(requests)
    assert [completion.state for completion in completions] == [
        "operation_complete",
        "operation_complete",
    ]
    assert [completion.result for completion in completions] == [0, 0]
    assert [completion.flags for completion in completions] == [0, 0]
    assert [completion.final for completion in completions] == [True, True]
    with pytest.raises(AttributeError):
        completions[0].result = 1
    assert ring.pending == 0
    assert [request.state for request in requests] == ["released", "released"]
    assert [request.done for request in requests] == [True, True]
    assert [request.cancelled for request in requests] == [False, False]
    assert [request.result for request in requests] == [0, 0]
    assert [request.flags for request in requests] == [0, 0]


def test_native_ring_keeps_request_alive_until_completion():
    ring = _uringcore.Ring(8)

    ring.prepare_nop()
    assert ring.submit() == 1

    [completion] = reap_completions(ring, 1)
    request = completion.request
    assert request.state == "released"
    assert request.result == 0


def test_native_ring_close_releases_prepared_request_ownership():
    ring = _uringcore.Ring(8)
    request = ring.prepare_nop()

    ring.close()

    assert ring.pending == 0
    assert request.state == "released"
    assert request.done is True
    assert request.cancelled is True
    assert request.result == -errno.ECANCELED


@pytest.mark.parametrize("max_completions", [-1, 0, 2**32, 2**64])
def test_native_ring_reap_rejects_out_of_range_batch_size(
    max_completions,
):
    ring = _uringcore.Ring(8)

    with pytest.raises(ValueError, match="max_completions must be between"):
        ring.reap(max_completions)


@pytest.mark.parametrize("delay_ns", [-1, 2**64, 1.5])
def test_native_ring_rejects_invalid_timeout(delay_ns):
    ring = _uringcore.Ring(8)

    error = TypeError if isinstance(delay_ns, float) else ValueError
    with pytest.raises(error):
        ring.prepare_timeout(delay_ns)


def test_native_ring_timeout_owns_timespec_until_completion():
    ring = _uringcore.Ring(8)
    request = ring.prepare_timeout(1_000_000)

    assert request.kind == "timeout"
    assert ring.submit() == 1
    [completion] = reap_completions(ring, 1)

    assert completion.request is request
    assert completion.result == -errno.ETIME
    assert completion.final is True
    assert request.state == "released"
    assert request.result == -errno.ETIME
    assert request.cancelled is False


def test_native_ring_models_asynchronous_cancellation_race():
    ring = _uringcore.Ring(8)
    target = ring.prepare_timeout(60_000_000_000)
    assert ring.submit() == 1

    cancellation = ring.prepare_cancel(target)
    assert target.state == "cancel_pending"
    assert cancellation.kind == "cancel"
    assert ring.submit() == 1

    completions = reap_completions(ring, 2)
    by_request = {completion.request: completion for completion in completions}

    assert by_request[cancellation].result == 0
    assert by_request[target].result == -errno.ECANCELED
    assert target.state == "released"
    assert target.cancelled is True
    assert cancellation.state == "released"
    assert cancellation.cancelled is False
    assert ring.pending == 0


def test_native_ring_rejects_invalid_cancellation_targets():
    ring = _uringcore.Ring(8)
    other_ring = _uringcore.Ring(8)
    prepared = ring.prepare_nop()
    foreign = other_ring.prepare_nop()

    with pytest.raises(ValueError, match="must be submitted"):
        ring.prepare_cancel(prepared)
    with pytest.raises(ValueError, match="not pending on this ring"):
        ring.prepare_cancel(foreign)

    assert ring.submit() == 1
    cancellation = ring.prepare_cancel(prepared)
    with pytest.raises(ValueError, match="not already cancelling"):
        ring.prepare_cancel(prepared)

    ring.close()
    other_ring.close()
    assert prepared.state == "released"
    assert cancellation.state == "released"


@pytest.mark.parametrize("cq_entries", [-1, 0, 4, 2**32])
def test_native_ring_rejects_invalid_cq_size(cq_entries):
    with pytest.raises(ValueError, match="cq_entries"):
        _uringcore.Ring(entries=8, cq_entries=cq_entries)


def test_native_ring_runtime_opcode_probe_can_be_injected():
    supported = {_uringcore.OP_NOP, _uringcore.OP_TIMEOUT}
    ring = _uringcore.Ring(8, supported_opcodes=supported)

    assert ring.supported_opcodes == frozenset(supported)
    assert ring.supports(_uringcore.OP_NOP) is True
    assert ring.supports(_uringcore.OP_ASYNC_CANCEL) is False

    ring.prepare_nop()
    with pytest.raises(NotImplementedError, match="ASYNC_CANCEL"):
        ring.prepare_cancel(ring.prepare_timeout(1_000_000))
    ring.close()


def test_native_ring_injected_probe_exercises_unsupported_fallback():
    ring = _uringcore.Ring(8, supported_opcodes=())

    with pytest.raises(NotImplementedError, match="NOP"):
        ring.prepare_nop()
    with pytest.raises(NotImplementedError, match="TIMEOUT"):
        ring.prepare_timeout(1)


@pytest.mark.parametrize("opcode", [-1, 2**32, "NOP"])
def test_native_ring_rejects_invalid_probe_values(opcode):
    error = TypeError if isinstance(opcode, str) else ValueError
    with pytest.raises(error):
        _uringcore.Ring(8, supported_opcodes={opcode})


def test_native_ring_detects_and_drains_cq_pressure():
    ring = _uringcore.Ring(entries=2, cq_entries=2)
    requests = []
    for _ in range(16):
        requests.extend((ring.prepare_nop(), ring.prepare_nop()))
        assert ring.submit() == 2

    deadline = time.monotonic() + 1
    while not ring.cq_overflowed and time.monotonic() < deadline:
        time.sleep(0.001)

    assert ring.cq_overflowed is True
    completions = reap_completions(ring, len(requests))
    assert {completion.request for completion in completions} == set(requests)
    assert ring.pending == 0
    assert ring.cq_dropped == 0
    assert ring.cq_overflowed is False


def test_native_ring_registers_bounded_provided_buffer_pool():
    ring = _uringcore.Ring(8)

    ring.setup_buffer_pool(entries=8, buffer_size=4096, group_id=7)

    assert ring.buffer_pool_entries == 8
    assert ring.buffer_size == 4096
    assert ring.buffer_group_id == 7
    assert ring.buffer_pool_bytes == 8 * 4096
    assert ring.buffers_available == 8
    assert ring.buffers_in_use == 0
    with pytest.raises(RuntimeError, match="already registered"):
        ring.setup_buffer_pool(8, 4096)

    ring.close()
    assert ring.buffer_pool_entries == 0
    assert ring.buffer_pool_bytes == 0


@pytest.mark.parametrize("entries", [0, 3, 32769, 2**32])
def test_native_ring_rejects_invalid_buffer_pool_entries(entries):
    ring = _uringcore.Ring(8)

    with pytest.raises(ValueError, match="entries"):
        ring.setup_buffer_pool(entries, 1024)


@pytest.mark.parametrize("buffer_size", [-1, 0, 2**32])
def test_native_ring_rejects_invalid_buffer_size(buffer_size):
    ring = _uringcore.Ring(8)

    with pytest.raises(ValueError, match="buffer_size"):
        ring.setup_buffer_pool(8, buffer_size)


@pytest.mark.parametrize("group_id", [-1, 65536, 2**64])
def test_native_ring_rejects_invalid_buffer_group(group_id):
    ring = _uringcore.Ring(8)

    with pytest.raises(ValueError, match="group_id"):
        ring.setup_buffer_pool(8, 1024, group_id)
