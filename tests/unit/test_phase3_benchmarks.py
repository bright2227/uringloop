"""Pure validation of transport benchmark framing, matrix, and failure schema."""

from asyncio import base_events
from dataclasses import asdict
import errno
import json
import subprocess

import pytest

from benchmarks import phase3


class NoIoLoop(base_events.BaseEventLoop):
    def run_until_complete(self, coroutine):
        # worker_result cleanup is the only coroutine in the mocked worker.
        coroutine.close()


class FakeTransport:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.writes = []
        self.aborted = False
        self.reading_paused = False
        self.limits = None

    def get_extra_info(self, name):
        return None

    def write(self, data):
        self.writes.append(bytes(data))

    def set_write_buffer_limits(self, *, high, low):
        self.limits = (low, high)

    def pause_reading(self):
        self.reading_paused = True

    def resume_reading(self):
        self.reading_paused = False

    def is_closing(self):
        return self.aborted

    def abort(self):
        self.aborted = True
        self.endpoint.connection_lost(None)


@pytest.fixture
def no_io_loop(monkeypatch):
    loop = NoIoLoop()
    monkeypatch.setattr(phase3.asyncio, "get_running_loop", lambda: loop)
    try:
        yield loop
    finally:
        loop.close()


def make_echo(protocol, *, slow_peer=False):
    workload = phase3.TransportWorkload(protocol, "tcp", 1, 32, 3, slow_peer)
    payloads = [phase3.payload_for(workload.message_size, sequence) for sequence in range(workload.rounds)]
    failures = []
    cls = phase3.FramedProtocol if protocol == "protocol" else phase3.FramedBufferedProtocol
    endpoint = cls(workload, payloads, echo=True, failures=failures)
    transport = FakeTransport(endpoint)
    endpoint.connection_made(transport)
    return endpoint, transport, payloads, failures


def feed(endpoint, data):
    if isinstance(endpoint, phase3.FramedProtocol):
        endpoint.data_received(data)
    else:
        buffer = endpoint.get_buffer(len(data))
        buffer[: len(data)] = data
        endpoint.buffer_updated(len(data))


def test_smoke_matrix_separates_protocol_network_and_slow_peer_modes():
    workloads = phase3.matrix("smoke")

    assert len(workloads) == 8
    assert {(item.protocol, item.network, item.slow_peer) for item in workloads} == {
        (protocol, network, slow_peer)
        for protocol in ("protocol", "buffered")
        for network in ("tcp", "unix")
        for slow_peer in (False, True)
    }
    assert all(item.message_size >= 262144 and item.peer_delay_us > 0 for item in workloads if item.slow_peer)


def test_full_matrix_preserves_connection_and_payload_dimensions():
    workloads = phase3.matrix("full")

    assert len(workloads) == 72
    assert {item.connections for item in workloads} == {1, 64, 1000}
    assert {item.message_size for item in workloads} == {64, 4096, 262144}
    assert all(item.operations == item.connections * item.rounds for item in workloads)


def test_native_transport_selection_is_separate_from_ring_backend():
    workloads = phase3.matrix("smoke", "native")
    assert all(item.transport_mode == "native" for item in workloads)
    command = phase3.worker_command("native-phase2", workloads[0], warmup=0, repeats=1)
    args = phase3.build_parser().parse_args(command[3:])
    assert args.transports == "native"
    assert args.backend == "native-phase2"
    assert phase3.build_parser().parse_args([]).transports == "python"


@pytest.mark.parametrize("backend", ["cffi", "cffi-phase2", "asyncio", "uvloop"])
def test_incompatible_native_transport_backend_fails_before_loop_allocation(monkeypatch, backend):
    workload = phase3.TransportWorkload("protocol", "tcp", 1, 32, 1, transport_mode="native")
    monkeypatch.setattr(phase3, "make_loop", lambda backend: pytest.fail("must validate before allocating a loop"))
    result = phase3.worker_result(backend, workload, warmup=0, repeats=1)
    assert result["status"] == "error"
    assert result["samples"] == []
    assert "native ring backend" in result["error"]["message"]


@pytest.mark.parametrize("protocol", ["protocol", "buffered"])
def test_echo_verifies_fragmented_and_coalesced_frames(no_io_loop, protocol):
    endpoint, transport, payloads, failures = make_echo(protocol)
    stream = b"".join(payloads)
    for start in range(0, len(stream), 13):
        feed(endpoint, stream[start : start + 13])

    assert transport.writes == payloads
    assert endpoint.sequence == 3
    assert endpoint.received == b""
    assert not failures
    endpoint.connection_lost(None)


@pytest.mark.parametrize("protocol", ["protocol", "buffered"])
@pytest.mark.parametrize("failure", ["corrupt", "reordered", "extra"])
def test_echo_rejects_corruption_reordering_and_extra_frames(no_io_loop, protocol, failure):
    endpoint, transport, payloads, failures = make_echo(protocol)
    if failure == "extra":
        feed(endpoint, b"".join(payloads))
        bad = payloads[-1]
    elif failure == "reordered":
        bad = payloads[1]
    else:
        bad = payloads[0][:-1] + bytes([payloads[0][-1] ^ 1])

    feed(endpoint, bad)

    assert transport.aborted
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert "payload mismatch" in str(failures[0])


@pytest.mark.parametrize("protocol", ["protocol", "buffered"])
def test_eof_with_partial_frame_is_a_failure(no_io_loop, protocol):
    endpoint, transport, payloads, failures = make_echo(protocol)
    feed(endpoint, payloads[0][:7])

    assert endpoint.eof_received() is False
    assert transport.aborted
    assert len(failures) == 1
    assert isinstance(failures[0], EOFError)


def test_slow_peer_pauses_receive_and_records_flow_callbacks(no_io_loop):
    endpoint, transport, _, _ = make_echo("buffered", slow_peer=True)
    handle = endpoint.resume_handle

    assert transport.reading_paused
    assert transport.limits == (8 * 1024, 32 * 1024)
    endpoint.pause_writing()
    assert not endpoint.writable.is_set()
    endpoint.resume_writing()
    assert endpoint.writable.is_set()
    assert (endpoint.pause_count, endpoint.resume_count) == (1, 1)
    endpoint.connection_lost(None)
    assert handle.cancelled()


def test_repeated_slow_peer_delay_cancels_previous_resume(no_io_loop):
    endpoint, _, _, _ = make_echo("protocol", slow_peer=True)
    previous = endpoint.resume_handle
    endpoint._delay_reading()
    assert previous.cancelled()
    current = endpoint.resume_handle
    assert current is not previous
    endpoint.connection_lost(None)
    assert current.cancelled()


def test_socket_setup_permission_error_is_preserved(no_io_loop):
    endpoint, _, _, failures = make_echo("protocol", slow_peer=True)

    class DeniedSocket:
        def setsockopt(self, *args):
            raise PermissionError(errno.EPERM, "socket option denied")

    class DeniedTransport(FakeTransport):
        def get_extra_info(self, name):
            return DeniedSocket()

    transport = DeniedTransport(endpoint)
    endpoint.connection_made(transport)

    assert transport.aborted
    assert failures[0].errno == errno.EPERM
    result = phase3.error_result("native", endpoint.workload, failures[0])
    assert result["status"] == "unsupported"
    assert result["samples"] == []
    assert result["summary"] is None


def worker_outcome(workload):
    return {
        "latencies": [1000] * workload.operations,
        "transport_classes": ["uringloop.stream_transport.IoUringSocketTransport"],
        "pause_writing_calls": 1,
        "resume_writing_calls": 1,
        "verified_bytes": workload.operations * workload.message_size * 2,
    }


def test_mocked_worker_records_actual_transport_core_and_verified_samples(monkeypatch):
    workload = phase3.TransportWorkload("buffered", "unix", 2, 32, 3)
    loop = NoIoLoop()
    loop._loop_core_mode = "phase2"
    monkeypatch.setattr(phase3, "make_loop", lambda backend: loop)
    monkeypatch.setattr(phase3, "run_workload", lambda loop, item: worker_outcome(item))

    result = phase3.worker_result("native-phase2", workload, warmup=1, repeats=2)

    assert result["status"] == "ok"
    assert result["runtime"]["loop_core"] == "phase2"
    assert result["runtime"]["transport_classes"] == ["uringloop.stream_transport.IoUringSocketTransport"]
    assert result["runtime"]["uringloop_transport_implementation"] == "python-counted-send"
    assert result["runtime"]["buffered_protocol_strategy"] == "application-staging-buffer"
    assert result["summary"]["latency_samples"] == 12
    assert len(result["samples"]) == 2
    assert all(sample["verified_bytes"] == 384 for sample in result["samples"])
    assert loop.is_closed()
    phase3.validate_result(result, "native-phase2", workload, 2)


def test_worker_discards_partial_measurements_after_permission_failure(monkeypatch):
    workload = phase3.TransportWorkload("protocol", "tcp", 1, 32, 1)
    loop = NoIoLoop()
    calls = 0

    def run(loop, item):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError(errno.EPERM, "io_uring_setup denied")
        return worker_outcome(item)

    monkeypatch.setattr(phase3, "make_loop", lambda backend: loop)
    monkeypatch.setattr(phase3, "run_workload", run)

    result = phase3.worker_result("native", workload, warmup=0, repeats=2)

    assert result["status"] == "unsupported"
    assert result["error"]["errno"] == errno.EPERM
    assert result["samples"] == []
    assert result["summary"] is None
    assert loop.is_closed()


def test_worker_rejects_empty_synthetic_success(monkeypatch):
    workload = phase3.TransportWorkload("protocol", "tcp", 1, 32, 1)
    loop = NoIoLoop()
    outcome = worker_outcome(workload)
    outcome["latencies"] = []
    monkeypatch.setattr(phase3, "make_loop", lambda backend: loop)
    monkeypatch.setattr(phase3, "run_workload", lambda loop, item: outcome)

    result = phase3.worker_result("asyncio", workload, warmup=0, repeats=1)

    assert result["status"] == "error"
    assert result["samples"] == []
    assert result["summary"] is None


@pytest.mark.parametrize("denied", [False, True])
@pytest.mark.parametrize("cleanup_denied", [False, True])
def test_cleanup_failure_never_overwrites_denial_or_reports_success(monkeypatch, denied, cleanup_denied):
    workload = phase3.TransportWorkload("protocol", "tcp", 1, 32, 1)

    class BrokenCleanupLoop(NoIoLoop):
        def close(self):
            super().close()
            if cleanup_denied:
                raise PermissionError(errno.EPERM, "cleanup failed")
            raise RuntimeError("cleanup failed")

    loop = BrokenCleanupLoop()
    monkeypatch.setattr(phase3, "make_loop", lambda backend: loop)

    def run(loop, item):
        if denied:
            raise PermissionError(errno.EPERM, "io_uring_setup denied")
        return worker_outcome(item)

    monkeypatch.setattr(phase3, "run_workload", run)
    result = phase3.worker_result("native", workload, warmup=0, repeats=1)
    assert result["status"] == ("unsupported" if denied else "error")
    assert result["samples"] == []
    assert result["summary"] is None
    assert result["cleanup_errors"][0]["message"].endswith("cleanup failed")
    assert result["cleanup_errors"][0]["errno"] == (errno.EPERM if cleanup_denied else None)
    if denied:
        assert result["error"]["errno"] == errno.EPERM
    assert loop.is_closed()


def test_worker_command_preserves_explicit_phase2_and_slow_peer_options():
    workload = phase3.TransportWorkload("buffered", "unix", 3, 262144, 2, True, 2500)
    command = phase3.worker_command("cffi-phase2", workload, warmup=1, repeats=3)
    args = phase3.build_parser().parse_args(command[3:])

    assert args.worker and args.slow_peer
    assert args.backend == "cffi-phase2"
    assert (args.protocol, args.network, args.peer_delay_us) == ("buffered", "unix", 2500)
    assert args.repeats == 3
    assert "phase3-native" not in phase3.BACKENDS
    assert "native-phase2" not in phase3.build_parser().parse_args([]).backends


def test_isolated_permission_failure_remains_unsupported(monkeypatch):
    workload = phase3.TransportWorkload("protocol", "tcp", 1, 32, 1)
    failure = phase3.error_result("native", workload, PermissionError(errno.EPERM, "denied"))
    process = subprocess.CompletedProcess([], 2, stdout=json.dumps(failure), stderr="")
    monkeypatch.setattr(phase3.subprocess, "run", lambda *args, **kwargs: process)

    assert phase3.run_isolated("native", workload, 0, 1) == failure


@pytest.mark.parametrize("kind", ["empty", "malformed", "timeout", "mismatched"])
def test_isolated_worker_failure_is_never_success(monkeypatch, kind):
    workload = phase3.TransportWorkload("protocol", "tcp", 1, 32, 1)

    def run(*args, **kwargs):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(args[0], 180)
        output = ""
        if kind == "malformed":
            output = "not JSON"
        if kind == "mismatched":
            failure = phase3.error_result("cffi", workload, RuntimeError("failed"))
            output = json.dumps(failure)
        return subprocess.CompletedProcess([], 1, stdout=output, stderr="worker failed")

    monkeypatch.setattr(phase3.subprocess, "run", run)
    result = phase3.run_isolated("native", workload, 0, 1)

    assert result["status"] == "error"
    assert result["samples"] == []
    assert result["summary"] is None


def test_result_validator_rejects_success_without_measurements():
    workload = phase3.TransportWorkload("protocol", "tcp", 1, 32, 1)
    result = {"status": "ok", "backend": "native", "workload": asdict(workload), "samples": [], "summary": {}}

    with pytest.raises(ValueError, match="measured samples"):
        phase3.validate_result(result, "native", workload, 1)
