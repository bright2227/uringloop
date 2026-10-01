import pytest

from benchmarks.eventfd_wakeup import BACKENDS as WAKEUP_BACKENDS, WakeupWorkload, worker_result as wakeup_worker_result
from benchmarks.phase1 import Workload, matrix, parse_strace_summary, percentile
from benchmarks.phase2 import SchedulingWorkload, matrix as scheduling_matrix, worker_result
from benchmarks.ready_queue import ReadyQueueWorkload, backends_for, worker_result as ready_queue_worker_result, workloads
from benchmarks.run_loop_tick import READY_BUDGET, TIMER_BUDGET, TickWorkload, worker_result as tick_worker_result
from benchmarks.timer_heap import TimerHeapWorkload, worker_result as timer_heap_worker_result
from scripts.compat_baseline import adaptation_methods_for_source, summarize, verify_adaptation_coverage


def test_full_benchmark_matrix_covers_phase_zero_dimensions():
    workloads = matrix("full")

    assert len(workloads) == 20
    assert {item.connections for item in workloads if item.api != "file"} == {1, 64, 1000}
    assert {item.message_size for item in workloads} == {64, 4096, 262144}
    assert {item.api for item in workloads} == {"socket", "streams", "file"}
    assert Workload("socket", 1000, 262144, 5) in workloads


def test_benchmark_percentiles_are_reported_in_microseconds():
    assert percentile([1_000, 2_000, 3_000], 0.5) == 2.0
    assert percentile([1_000, 2_000, 3_000], 0.99) == 3.0


def test_benchmark_operation_counts_match_each_api():
    assert Workload("socket", 4, 64, 10).operations == 40
    assert Workload("streams", 4, 64, 10).operations == 40
    assert Workload("file", 1, 4096, 10).operations == 10


def test_full_scheduling_matrix_covers_phase_two_dimensions():
    workloads = scheduling_matrix("full")

    assert {item.kind for item in workloads} == {
        "ready",
        "timer_equal",
        "timer_spread",
        "timer_cancel",
        "mixed",
        "threadsafe_idle",
        "threadsafe_burst",
    }
    assert {item.operations for item in workloads if item.kind == "ready"} == {1, 64, 4096}
    assert {item.operations for item in workloads if item.kind == "timer_equal"} == {1, 1000, 100000}
    assert {item.producers for item in workloads if item.kind == "threadsafe_burst"} == {1, 4}
    [idle] = [item for item in workloads if item.kind == "threadsafe_idle"]
    assert idle.producers == 1
    assert idle.idle_settle_ns > 0
    assert all(item.lead_ns > 0 and item.spread_ns >= 2_000_000_000 for item in workloads if item.kind == "timer_spread")
    assert SchedulingWorkload("timer_cancel", 100000, cancel_every=2) in workloads


def test_scheduling_worker_reports_ready_and_cancelled_timer_samples():
    ready = worker_result("asyncio", SchedulingWorkload("ready", 8), warmup=0, repeats=2)
    timers = worker_result(
        "asyncio",
        SchedulingWorkload("timer_cancel", 8, cancel_every=2),
        warmup=0,
        repeats=2,
    )

    assert len(ready["samples"]) == 2
    assert ready["summary"]["latencies"]["ready"]["samples"] == 16
    assert timers["summary"]["latencies"]["timer"]["samples"] == 8
    assert all(sample["operations_per_second"] > 0 for sample in ready["samples"] + timers["samples"])


def test_ready_queue_benchmark_matrix_compares_native_and_python_paths():
    full = workloads("full")

    assert {item.kind for item in full} == {"queue", "loop"}
    assert {item.operations for item in full} == {1, 64, 4096, 100_000}
    assert backends_for(ReadyQueueWorkload("queue", 1)) == ("native-core", "python-deque")
    assert backends_for(ReadyQueueWorkload("loop", 1)) == ("native-loop", "python-loop")


def test_ready_queue_worker_reports_enqueue_and_batch_throughput():
    result = ready_queue_worker_result(
        "native-core",
        ReadyQueueWorkload("queue", 32),
        warmup=0,
        repeats=2,
    )

    assert len(result["samples"]) == 2
    assert result["summary"]["enqueue_operations_per_second_median"] > 0
    assert result["summary"]["drain_operations_per_second_median"] > 0


@pytest.mark.parametrize("backend", ["native-loop", "python-loop"])
def test_ready_queue_worker_reports_kernel_independent_loop_dispatch(backend):
    result = ready_queue_worker_result(
        backend,
        ReadyQueueWorkload("loop", 32),
        warmup=0,
        repeats=2,
    )

    assert len(result["samples"]) == 2
    assert result["summary"]["latency_samples"] == 64
    assert result["summary"]["operations_per_second_median"] > 0


@pytest.mark.parametrize("backend", ["native-core", "python-heap"])
def test_timer_heap_worker_reports_insert_cancel_and_expiry_phases(backend):
    result = timer_heap_worker_result(backend, TimerHeapWorkload(32), warmup=0, repeats=2)

    assert len(result["samples"]) == 2
    assert result["summary"]["insert_seconds_median"] >= 0
    assert result["summary"]["cancel_seconds_median"] >= 0
    assert result["summary"]["expire_seconds_median"] >= 0
    assert result["summary"]["operations_per_second_median"] > 0


def test_wakeup_worker_reports_idle_latency_and_burst_write_counts():
    result = wakeup_worker_result("eventfd", WakeupWorkload(4, 32, 2), warmup=0, repeats=2)

    assert WAKEUP_BACKENDS == ("eventfd", "socketpair")
    assert len(result["samples"]) == 2
    assert result["summary"]["idle_latency_p99_us_median"] > 0
    assert result["summary"]["idle_writes_median"] == 4
    assert result["summary"]["burst_writes_median"] >= 1


@pytest.mark.parametrize("backend", ["native-core", "python-model"])
def test_run_loop_tick_worker_reports_fairness_budget_counts(backend):
    result = tick_worker_result(
        backend,
        TickWorkload(READY_BUDGET + 1, TIMER_BUDGET + 1),
        warmup=0,
        repeats=2,
    )

    assert len(result["samples"]) == 2
    assert result["budgets"] == {"ready": READY_BUDGET, "timers": TIMER_BUDGET}
    assert result["summary"]["ticks_median"] >= 2
    assert result["summary"]["ready_budget_exhaustions_median"] > 0
    assert result["summary"]["timer_budget_exhaustions_median"] > 0


def test_strace_summary_extracts_per_syscall_counts(tmp_path):
    summary = tmp_path / "strace.txt"
    summary.write_text(
        "% time seconds usecs/call calls errors syscall\n"
        " 60.00 0.001 10 12 2 io_uring_enter\n"
        " 40.00 0.001 5 8 epoll_wait\n"
        "100.00 0.002 8 20 2 total\n"
    )

    parsed = parse_strace_summary(summary)

    assert parsed["calls"] == {"io_uring_enter": 12, "epoll_wait": 8}
    assert parsed["total_calls"] == 20


def test_compatibility_baseline_records_skip_reasons(tmp_path):
    report = tmp_path / "compat.xml"
    report.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="1" time="0.1">'
        '<testcase classname="compat" name="passing" />'
        '<testcase classname="compat" name="skipped"><skipped message="documented reason" /></testcase>'
        "</testsuite>"
    )

    result = summarize(report)

    assert result["passed"] == 1
    assert result["pass_rate_percent"] == 50
    assert result["skip_reasons"] == [{"test": "compat.skipped", "reason": "documented reason"}]


def test_compatibility_provenance_requires_referenced_method():
    adaptations = {
        "test_local_contract": {
            "summary": "Local adaptation summary.",
            "upstream": "test_events.py::test_upstream_contract",
        }
    }
    source = "class ContractTests:\n    def test_upstream_contract(self):\n        pass\n"

    assert adaptation_methods_for_source("test_events.py", source, adaptations) == ["ContractTests::test_upstream_contract"]

    with pytest.raises(RuntimeError, match="was not found"):
        adaptations["test_local_contract"]["upstream"] = "test_events.py::missing"
        adaptation_methods_for_source("test_events.py", source, adaptations)


def test_compatibility_manifest_covers_every_local_contract():
    import json
    from pathlib import Path

    manifest = json.loads((Path(__file__).parents[1] / "compat" / "upstream.json").read_text())

    verify_adaptation_coverage(manifest["adaptations"])
