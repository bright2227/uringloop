import errno
import gc
import random
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import weakref

import pytest

from uringloop import _uringcore


def test_native_core_has_versioned_abi():
    assert _uringcore.ABI_VERSION == 10
    assert _uringcore.LoopCore.__module__ == "uringloop._uringcore"
    assert _uringcore.Ring.__module__ == "uringloop._uringcore"
    assert _uringcore.Request.__module__ == "uringloop._uringcore"
    assert _uringcore.Completion.__module__ == "uringloop._uringcore"
    assert _uringcore.ProvidedBuffer.__module__ == "uringloop._uringcore"


def test_loop_core_ready_queue_is_fifo_and_bounded_by_snapshot():
    core = _uringcore.LoopCore()
    handles = [object() for _ in range(6)]
    for handle in handles:
        core.enqueue_ready(handle)

    assert core.ready_count == 6
    assert core.take_ready_batch(2) == handles[:2]
    core.enqueue_ready(handles[0])
    assert core.take_ready_batch(99) == [*handles[2:], handles[0]]
    assert core.ready_count == 0


def test_loop_core_zero_limit_is_safe_for_empty_and_populated_queues():
    core = _uringcore.LoopCore()

    assert core.take_ready_batch(0) == []
    handle = object()
    core.enqueue_ready(handle)
    assert core.take_ready_batch(0) == []
    assert core.take_ready_batch(1) == [handle]


def test_loop_core_resizes_without_losing_ready_handles():
    core = _uringcore.LoopCore()
    handles = [object() for _ in range(10_000)]
    for handle in handles:
        core.enqueue_ready(handle)

    assert core.take_ready_batch(len(handles)) == handles


def test_loop_core_enqueue_failure_is_atomic():
    core = _uringcore.LoopCore(max_ready=2)
    first = object()
    second = object()
    core.enqueue_ready(first)
    core.enqueue_ready(second)

    with pytest.raises(MemoryError):
        core.enqueue_ready(object())

    assert core.ready_count == 2
    assert core.take_ready_batch(2) == [first, second]


def test_loop_core_clear_and_close_release_owned_references():
    class Handle:
        pass

    core = _uringcore.LoopCore()
    handle = Handle()
    reference = weakref.ref(handle)
    core.enqueue_ready(handle)
    del handle
    assert reference() is not None

    core.clear_ready()
    core.clear_ready()
    assert reference() is None
    assert core.ready_count == 0

    core.close()
    core.close()
    assert core.closed is True
    with pytest.raises(RuntimeError, match="cannot be reinitialized"):
        core.__init__()
    with pytest.raises(RuntimeError, match="loop core is closed"):
        core.enqueue_ready(object())
    with pytest.raises(RuntimeError, match="loop core is closed"):
        core.take_ready_batch(1)
    with pytest.raises(RuntimeError, match="loop core is closed"):
        _ = core.ready_count


def test_loop_core_ready_handle_cycle_is_visible_to_gc():
    class Handle:
        pass

    core = _uringcore.LoopCore()
    handle = Handle()
    handle.core = core
    reference = weakref.ref(handle)
    core.enqueue_ready(handle)

    del handle
    del core
    gc.collect()

    assert reference() is None


@pytest.mark.parametrize("limit", [-1, -(2**63)])
def test_loop_core_rejects_negative_batch_limit(limit):
    core = _uringcore.LoopCore()

    with pytest.raises(ValueError, match="cannot be negative"):
        core.take_ready_batch(limit)


class Timer:
    def __init__(self, name):
        self.name = name
        self._cancelled = False
        self._scheduled = True

    def cancel(self):
        self._cancelled = True


def test_loop_core_timer_heap_orders_deadlines_and_promotes_a_bounded_batch():
    core = _uringcore.LoopCore()
    later = Timer("later")
    first = Timer("first")
    equal = Timer("equal")
    core.schedule_timer(later, 12.0)
    core.schedule_timer(first, 10.0)
    core.schedule_timer(equal, 10.0)

    assert core.timer_count == 3
    assert core.next_deadline() == 10.0
    assert core.promote_due(10.0, 0.5, 1) == 1
    assert core.take_ready_batch(10) == [first]
    assert first._scheduled is False
    assert core.next_deadline() == 10.0
    assert core.promote_due(10.0, 0.5, 10) == 1
    assert core.take_ready_batch(10) == [equal]
    assert core.next_deadline() == 12.0


def test_loop_core_timer_cancellation_prunes_roots_and_compacts_storms():
    core = _uringcore.LoopCore()
    timers = [Timer(index) for index in range(200)]
    for index, timer in enumerate(timers):
        core.schedule_timer(timer, float(index))
        if index != 199:
            timer.cancel()

    assert core.cancelled_timer_count == 199
    assert core.next_deadline() == 199.0
    assert core.timer_count == 1

    extra = [Timer(index) for index in range(20)]
    for index, timer in enumerate(extra):
        core.schedule_timer(timer, 300.0 + index)
        if index % 2 == 0:
            timer.cancel()
    assert core.compact_timers() == 10
    assert core.cancelled_timer_count == 0
    assert core.timer_count == 11


def test_loop_core_timer_schedule_failure_is_atomic():
    core = _uringcore.LoopCore(max_timers=1)
    retained = Timer("retained")
    rejected = Timer("rejected")
    core.schedule_timer(retained, 2.0)

    with pytest.raises(MemoryError):
        core.schedule_timer(rejected, 1.0)

    assert core.timer_count == 1
    assert core.next_deadline() == 2.0


def test_loop_core_timer_heap_matches_reference_model_under_randomized_operations():
    generator = random.Random(20260810)
    core = _uringcore.LoopCore()
    entries = []
    for sequence in range(500):
        timer = Timer(sequence)
        when = float(generator.randrange(100))
        core.schedule_timer(timer, when)
        if generator.randrange(4) == 0:
            timer.cancel()
        entries.append((when, sequence, timer))

    core.compact_timers()
    promoted = []
    for now in range(101):
        while core.next_deadline() is not None and core.next_deadline() < now + 0.1:
            core.promote_due(float(now), 0.1, 7)
            promoted.extend(core.take_ready_batch(7))

    expected = [timer for when, sequence, timer in sorted(entries) if not timer._cancelled]
    assert promoted == expected
    assert core.timer_count == 0


def test_loop_core_timer_python_attribute_reentrancy_cannot_invalidate_native_storage():
    script = r"""
from uringloop import _uringcore

class ReentrantCancelled:
    def __init__(self, core, cancelled):
        object.__setattr__(self, "core", core)
        object.__setattr__(self, "cancelled", cancelled)
        object.__setattr__(self, "_scheduled", True)

    def __getattribute__(self, name):
        if name == "_cancelled":
            core = object.__getattribute__(self, "core")
            for mutation in (core.clear_ready, core.clear_timers, core.close):
                try:
                    mutation()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("reentrant mutation was not rejected")
            return object.__getattribute__(self, "cancelled")
        return object.__getattribute__(self, name)

class ReentrantScheduled(ReentrantCancelled):
    def __setattr__(self, name, value):
        if name == "_scheduled":
            core = object.__getattribute__(self, "core")
            try:
                core.clear_timers()
            except RuntimeError:
                pass
            else:
                raise AssertionError("reentrant timer clear was not rejected")
        object.__setattr__(self, name, value)

core = _uringcore.LoopCore()
cancelled = ReentrantCancelled(core, True)
core.schedule_timer(cancelled, 1.0)
assert core.next_deadline() is None
live = ReentrantScheduled(core, False)
core.schedule_timer(live, 2.0)
assert core.promote_due(3.0, 0.0, 1) == 1
assert core.take_ready_batch(1) == [live]
core.close()

core = _uringcore.LoopCore()
cancelled = ReentrantCancelled(core, True)
live = ReentrantCancelled(core, False)
core.schedule_timer(cancelled, 1.0)
core.schedule_timer(live, 2.0)
assert core.compact_timers() == 1
assert core.timer_count == 1
assert core.next_deadline() == 2.0
core.close()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_loop_core_timer_clear_and_gc_release_owned_handles():
    core = _uringcore.LoopCore()
    timer = Timer("owned")
    reference = weakref.ref(timer)
    core.schedule_timer(timer, 1.0)
    del timer
    assert reference() is not None
    core.clear_timers()
    assert reference() is None

    cyclic = Timer("cyclic")
    cyclic.core = core
    reference = weakref.ref(cyclic)
    core.schedule_timer(cyclic, 2.0)
    del cyclic
    del core
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize(
    ("now", "resolution", "limit", "message"),
    [(0.0, -1.0, 1, "resolution"), (0.0, 0.0, -1, "limit")],
)
def test_loop_core_timer_promotion_rejects_negative_bounds(now, resolution, limit, message):
    core = _uringcore.LoopCore()
    with pytest.raises(ValueError, match=message):
        core.promote_due(now, resolution, limit)


def reap_completions(ring, count):
    completed = []
    deadline = time.monotonic() + 1
    while len(completed) < count and time.monotonic() < deadline:
        completed.extend(ring.reap(count - len(completed)))
        if len(completed) < count:
            time.sleep(0.001)
    assert len(completed) == count
    return completed


def reap_request_final(ring, request):
    completions = []
    deadline = time.monotonic() + 1
    while not request.done and time.monotonic() < deadline:
        completions.extend(ring.reap())
        if not request.done:
            time.sleep(0.001)
    assert request.done
    return completions


def tcp_socket_pair():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    client = socket.create_connection(listener.getsockname())
    server, _ = listener.accept()
    listener.close()
    return client, server


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


@pytest.mark.parametrize("flags", [-1, 2**32])
def test_native_ring_rejects_out_of_range_setup_flags(flags):
    with pytest.raises(ValueError, match="flags must be between"):
        _uringcore.Ring(8, flags=flags)


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


def test_native_ring_waits_for_and_reaps_poll_completion():
    ring = _uringcore.Ring(8)
    receiver, sender = socket.socketpair()
    try:
        request = ring.prepare_poll(receiver, select.POLLIN)
        assert request.kind == "poll"
        assert ring.submit() == 1

        sender.sendall(b"ready")
        [completion] = ring.wait(timeout_ns=1_000_000_000)

        assert completion.request is request
        assert completion.result & select.POLLIN
        assert completion.final is True
        assert request.state == "released"
    finally:
        receiver.close()
        sender.close()
        ring.close()


def test_native_ring_wait_releases_gil_for_wakeup_thread():
    ring = _uringcore.Ring(8)
    receiver, sender = socket.socketpair()

    def wake_waiter():
        time.sleep(0.01)
        sender.sendall(b"ready")

    thread = threading.Thread(target=wake_waiter)
    try:
        request = ring.prepare_poll(receiver, select.POLLIN)
        assert ring.submit() == 1
        thread.start()

        [completion] = ring.wait(timeout_ns=1_000_000_000)

        assert completion.request is request
        assert completion.result & select.POLLIN
        thread.join(timeout=1)
        assert thread.is_alive() is False
    finally:
        if thread.ident is not None:
            thread.join(timeout=1)
        receiver.close()
        sender.close()
        ring.close()


def test_native_ring_wait_timeout_returns_without_completion():
    ring = _uringcore.Ring(8)

    assert ring.wait(timeout_ns=1_000_000) == []


def test_native_ring_wait_validates_batch_size_before_blocking():
    ring = _uringcore.Ring(8)
    started = time.monotonic()

    with pytest.raises(ValueError, match="max_completions must be between"):
        ring.wait(timeout_ns=1_000_000_000, max_completions=0)

    assert time.monotonic() - started < 0.1


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


def test_native_ring_models_completion_before_cancellation_race():
    ring = _uringcore.Ring(8)
    target = ring.prepare_nop()
    assert ring.submit() == 1
    deadline = time.monotonic() + 1
    while ring.cq_ready == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert ring.cq_ready == 1

    cancellation = ring.prepare_cancel(target)
    assert ring.submit() == 1
    completions = reap_completions(ring, 2)
    by_request = {completion.request: completion for completion in completions}

    assert by_request[target].result == 0
    assert by_request[cancellation].result == -errno.ENOENT
    assert target.cancelled is False
    assert target.state == "released"
    assert cancellation.state == "released"


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


def test_native_ring_send_and_recv_own_python_buffers():
    left, right = socket.socketpair()
    ring = _uringcore.Ring(8)
    received = bytearray(5)
    recv_request = ring.prepare_recv(left, received)
    send_request = ring.prepare_send(right, b"hello")

    assert ring.submit() == 2
    completions = reap_completions(ring, 2)
    by_request = {completion.request: completion for completion in completions}

    assert by_request[send_request].result == 5
    assert by_request[recv_request].result == 5
    assert received == b"hello"
    ring.close()
    left.close()
    right.close()


def test_native_ring_gc_breaks_file_owner_cycle():
    class RingFile:
        def __init__(self, ring, file):
            self.ring = ring
            self.file = file

        def fileno(self):
            return self.file.fileno()

    receiver, sender = socket.socketpair()
    buffer = bytearray(1)
    ring = _uringcore.Ring(8)
    owner = RingFile(ring, receiver)
    request = ring.prepare_recv(owner, buffer)
    assert ring.submit() == 1
    with pytest.raises(BufferError):
        buffer.extend(b"pinned")

    del request
    del owner
    del ring
    gc.collect()

    buffer.extend(b"released")
    receiver.close()
    sender.close()


def test_native_ring_reads_and_writes_files_with_owned_buffers():
    ring = _uringcore.Ring(8)
    with tempfile.TemporaryFile() as file:
        write_request = ring.prepare_write(file, b"uring", offset=0)
        assert ring.submit() == 1
        [write_completion] = reap_completions(ring, 1)
        assert write_completion.request is write_request
        assert write_completion.result == 5

        target = bytearray(5)
        read_request = ring.prepare_read(file, target, offset=0)
        assert ring.submit() == 1
        [read_completion] = reap_completions(ring, 1)
        assert read_completion.request is read_request
        assert read_completion.result == 5
        assert target == b"uring"


def test_native_multishot_recv_returns_explicitly_owned_buffers():
    left, right = socket.socketpair(type=socket.SOCK_DGRAM)
    ring = _uringcore.Ring(8)
    ring.setup_buffer_pool(4, 64)
    request = ring.prepare_recv_multishot(left)
    assert ring.submit() == 1

    right.send(b"one")
    right.send(b"two")
    completions = reap_completions(ring, 2)
    buffers = [completion.buffer for completion in completions]

    assert [buffer.to_bytes() for buffer in buffers] == [b"one", b"two"]
    assert all(completion.final is False for completion in completions)
    assert ring.buffers_available == 2
    assert ring.buffers_in_use == 2
    with pytest.raises(BufferError, match="provided buffers are in use"):
        ring.close()

    view = memoryview(buffers[0])
    assert view.tobytes() == b"one"
    with pytest.raises(BufferError, match="views are exported"):
        buffers[0].release()
    view.release()
    buffers[0].release()
    buffers[1].release()
    assert ring.buffers_available == 4
    assert ring.buffers_in_use == 0

    cancellation = ring.prepare_cancel(request)
    assert ring.submit() == 1
    final = reap_completions(ring, 2)
    assert {completion.request for completion in final} == {request, cancellation}
    ring.close()
    left.close()
    right.close()


def test_native_ring_context_exit_preserves_owned_provided_buffer():
    left, right = socket.socketpair(type=socket.SOCK_DGRAM)
    ring = _uringcore.Ring(8)
    request = None
    selected = None
    try:
        with pytest.raises(BufferError, match="provided buffers are in use"):
            with ring:
                ring.setup_buffer_pool(2, 64)
                request = ring.prepare_recv_multishot(left)
                assert ring.submit() == 1
                right.send(b"owned")
                [completion] = reap_completions(ring, 1)
                selected = completion.buffer

        assert ring.closed is False
        assert selected.to_bytes() == b"owned"
        selected.release()
        cancellation = ring.prepare_cancel(request)
        assert ring.submit() == 1
        assert {item.request for item in reap_completions(ring, 2)} == {
            request,
            cancellation,
        }
    finally:
        if selected is not None and not selected.released:
            selected.release()
        ring.close()
        left.close()
        right.close()


def test_native_multishot_recv_applies_backpressure_and_rearms():
    left, right = socket.socketpair(type=socket.SOCK_DGRAM)
    ring = _uringcore.Ring(8)
    ring.setup_buffer_pool(2, 64)
    request = ring.prepare_recv_multishot(left)
    assert ring.submit() == 1

    for payload in (b"one", b"two", b"blocked"):
        right.send(payload)
    completions = reap_request_final(ring, request)
    selected = [completion for completion in completions if completion.buffer]

    assert [completion.buffer.to_bytes() for completion in selected] == [
        b"one",
        b"two",
    ]
    assert completions[-1].result == -errno.ENOBUFS
    assert completions[-1].final is True
    assert ring.buffers_available == 0
    assert ring.buffers_in_use == 2

    for completion in selected:
        completion.buffer.release()
    rearmed = ring.prepare_recv_multishot(left)
    assert ring.submit() == 1
    [completion] = reap_completions(ring, 1)
    assert completion.request is rearmed
    assert completion.buffer.to_bytes() == b"blocked"
    completion.buffer.release()

    cancellation = ring.prepare_cancel(rearmed)
    assert ring.submit() == 1
    final = reap_completions(ring, 2)
    assert {item.request for item in final} == {rearmed, cancellation}
    ring.close()
    left.close()
    right.close()


def test_native_send_zc_retains_buffer_until_notification():
    left, right = tcp_socket_pair()
    ring = _uringcore.Ring(8)
    if not ring.supports(_uringcore.OP_SEND_ZC):
        pytest.skip("SEND_ZC is unavailable")
    payload = bytearray(b"z" * 4096)
    request = ring.prepare_send_zc(left, payload)
    assert ring.submit() == 1
    assert right.recv(len(payload)) == payload

    first = reap_completions(ring, 1)[0]
    completions = [first]
    if not first.final:
        assert request.state == "notification_pending"
        with pytest.raises(BufferError):
            payload.extend(b"still-owned")
        completions.extend(reap_request_final(ring, request))

    assert completions[0].state == "operation_complete"
    assert completions[0].result == len(payload)
    if len(completions) == 2:
        assert completions[0].final is False
        assert completions[1].state == "notification_pending"
        assert completions[1].final is True
    payload.extend(b"released")
    ring.close()
    left.close()
    right.close()
