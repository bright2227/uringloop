#!/usr/bin/env python3
"""Isolated Phase 2 native-handle design-gate benchmarks."""

import argparse
from asyncio import events
import contextvars
from dataclasses import asdict, dataclass
import gc
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time
import tracemalloc
from typing import Any

from benchmarks.phase1 import environment_metadata
from benchmarks.phase2 import WORKER_TIMEOUT_SECONDS
from uringloop import _uringcore


ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ("stock-python", "native-record-wrapper", "full-native-prototype")
KINDS = ("handle", "timer")
_PROTOTYPE_MODULE = None


def _prototypes():
    global _PROTOTYPE_MODULE
    if _PROTOTYPE_MODULE is not None:
        return _PROTOTYPE_MODULE
    try:
        from benchmarks import _native_handle_prototypes
    except ImportError as exc:
        raise RuntimeError(
            "build benchmark prototypes with URINGLOOP_BUILD_HANDLE_PROTOTYPES=1 python setup.py build_ext --inplace --force"
        ) from exc
    _PROTOTYPE_MODULE = _native_handle_prototypes
    return _PROTOTYPE_MODULE


class BenchmarkLoop:
    __slots__ = ("timer_cancellations",)

    def __init__(self):
        self.timer_cancellations = 0

    def get_debug(self):
        return False

    def _timer_handle_cancelled(self, handle):
        self.timer_cancellations += 1

    def call_exception_handler(self, context):
        raise AssertionError(context)


class RecordBackedHandle:
    __slots__ = ("_record", "_repr", "_source_traceback", "__weakref__")
    _record_type = None

    def __init__(self, callback, args, loop, context):
        self._record = self._record_type(callback, args, loop, context)
        self._source_traceback = None
        self._repr = None

    @property
    def _cancelled(self):
        return self._record.cancelled()

    def cancel(self):
        self._record.cancel()

    def cancelled(self):
        return self._record.cancelled()

    def get_context(self):
        return self._record.get_context()

    def _run(self):
        self._record.run()


class RecordBackedTimerHandle(RecordBackedHandle):
    __slots__ = ("_scheduled", "_when")

    def __init__(self, when, callback, args, loop, context):
        super().__init__(callback, args, loop, context)
        self._when = when
        self._scheduled = False

    def cancel(self):
        if not self.cancelled():
            self._record.cancel_timer(self)

    def when(self):
        return self._when


@dataclass(frozen=True, slots=True)
class HandleWorkload:
    kind: str
    operations: int


def workloads(profile: str):
    operations = (20_000,) if profile == "smoke" else (1, 1000, 100_000)
    return [HandleWorkload(kind, count) for count in operations for kind in KINDS]


def _factory(backend: str, kind: str, loop: BenchmarkLoop, callback, context):
    if backend == "stock-python":
        handle_type = events.Handle if kind == "handle" else events.TimerHandle
    elif backend == "native-record-wrapper":
        RecordBackedHandle._record_type = _prototypes().HandleRecord
        handle_type = RecordBackedHandle if kind == "handle" else RecordBackedTimerHandle
    elif backend == "full-native-prototype":
        prototypes = _prototypes()
        handle_type = prototypes.NativeHandle if kind == "handle" else prototypes.NativeTimerHandle
    else:
        raise ValueError(f"invalid handle backend: {backend}")

    if kind == "handle":
        return lambda index: handle_type(callback, (index,), loop, context)
    if kind == "timer":
        return lambda index: handle_type(float(index), callback, (index,), loop, context)
    raise ValueError(f"invalid handle kind: {kind}")


def _retained_shallow_bytes(handles):
    total = sum(sys.getsizeof(handle) for handle in handles)
    for handle in handles:
        record = getattr(handle, "_record", None)
        if record is not None:
            total += sys.getsizeof(record)
    return total


def handle_sample(backend: str, workload: HandleWorkload) -> dict[str, float | int]:
    loop = BenchmarkLoop()
    completed = []
    context = contextvars.Context()
    factory = _factory(backend, workload.kind, loop, completed.append, context)
    gc.collect()
    tracemalloc.start()
    blocks_before = sys.getallocatedblocks()
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    handles = [factory(index) for index in range(workload.operations)]
    if workload.kind == "timer":
        for handle in handles:
            handle._scheduled = True
    allocated_ns = time.perf_counter_ns()
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    allocated_blocks = max(0, sys.getallocatedblocks() - blocks_before)
    retained_shallow_bytes = _retained_shallow_bytes(handles)
    core = _uringcore.LoopCore(max_ready=workload.operations)
    if workload.kind == "handle":
        for handle in handles:
            core.enqueue_ready(handle)
    else:
        for index, handle in enumerate(handles):
            core.schedule_timer(handle, float(index))
        core.promote_due(float(workload.operations), 0.0, workload.operations)
    scheduled_handles = core.take_ready_batch(workload.operations)
    scheduled_ns = time.perf_counter_ns()
    if scheduled_handles != handles:
        raise RuntimeError("native storage changed handle identity or FIFO order")
    for handle in scheduled_handles:
        handle._run()
    dispatched_ns = time.perf_counter_ns()
    for handle in handles:
        handle.cancel()
        handle.cancel()
    cancelled_ns = time.perf_counter_ns()
    cpu_finished = time.process_time_ns()
    tracemalloc.stop()
    if completed != list(range(workload.operations)):
        raise RuntimeError("handle benchmark changed callback order or lost work")
    if not all(handle.cancelled() for handle in handles):
        raise RuntimeError("handle benchmark cancellation was not idempotent")
    expected_timer_cancellations = workload.operations if workload.kind == "timer" else 0
    if loop.timer_cancellations != expected_timer_cancellations:
        raise RuntimeError("timer cancellation notification count changed")
    allocation_ns = allocated_ns - wall_started
    storage_ns = scheduled_ns - allocated_ns
    dispatch_ns = dispatched_ns - scheduled_ns
    cancel_ns = cancelled_ns - dispatched_ns
    return {
        "wall_seconds": (cancelled_ns - wall_started) / 1_000_000_000,
        "cpu_seconds": (cpu_finished - cpu_started) / 1_000_000_000,
        "allocation_seconds": allocation_ns / 1_000_000_000,
        "storage_seconds": storage_ns / 1_000_000_000,
        "dispatch_seconds": dispatch_ns / 1_000_000_000,
        "cancel_seconds": cancel_ns / 1_000_000_000,
        "allocation_operations_per_second": workload.operations * 1_000_000_000 / max(1, allocation_ns),
        "storage_operations_per_second": workload.operations * 1_000_000_000 / max(1, storage_ns),
        "dispatch_operations_per_second": workload.operations * 1_000_000_000 / max(1, dispatch_ns),
        "allocated_blocks": allocated_blocks,
        "traced_current_bytes": current_bytes,
        "traced_peak_bytes": peak_bytes,
        "retained_shallow_bytes": retained_shallow_bytes,
    }


def worker_result(backend: str, workload: HandleWorkload, warmup: int, repeats: int) -> dict[str, Any]:
    if backend not in BACKENDS or workload.kind not in KINDS:
        raise ValueError("invalid native handle benchmark selection")
    for _ in range(warmup):
        handle_sample(backend, workload)
    samples = [handle_sample(backend, workload) for _ in range(repeats)]
    wall = [sample["wall_seconds"] for sample in samples]
    summary = {
        "wall_seconds_median": statistics.median(wall),
        "wall_time_cv_percent": statistics.stdev(wall) / statistics.mean(wall) * 100 if len(wall) > 1 else 0.0,
        "cpu_seconds_median": statistics.median(sample["cpu_seconds"] for sample in samples),
        "allocation_seconds_median": statistics.median(sample["allocation_seconds"] for sample in samples),
        "storage_seconds_median": statistics.median(sample["storage_seconds"] for sample in samples),
        "dispatch_seconds_median": statistics.median(sample["dispatch_seconds"] for sample in samples),
        "cancel_seconds_median": statistics.median(sample["cancel_seconds"] for sample in samples),
        "allocation_operations_per_second_median": statistics.median(
            sample["allocation_operations_per_second"] for sample in samples
        ),
        "storage_operations_per_second_median": statistics.median(
            sample["storage_operations_per_second"] for sample in samples
        ),
        "dispatch_operations_per_second_median": statistics.median(
            sample["dispatch_operations_per_second"] for sample in samples
        ),
        "allocated_blocks_median": statistics.median(sample["allocated_blocks"] for sample in samples),
        "traced_peak_bytes_median": statistics.median(sample["traced_peak_bytes"] for sample in samples),
        "retained_shallow_bytes_median": statistics.median(sample["retained_shallow_bytes"] for sample in samples),
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    return {
        "backend": backend,
        "workload": asdict(workload),
        "warmup_runs": warmup,
        "measured_runs": repeats,
        "samples": samples,
        "summary": summary,
    }


def run_isolated(backend, workload, warmup, repeats):
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.native_handles",
            "--worker",
            "--backend",
            backend,
            "--kind",
            workload.kind,
            "--operations",
            str(workload.operations),
            "--warmup",
            str(warmup),
            "--repeats",
            str(repeats),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=WORKER_TIMEOUT_SECONDS,
    )
    return json.loads(process.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--backend", choices=BACKENDS, default="stock-python")
    parser.add_argument("--kind", choices=KINDS, default="handle")
    parser.add_argument("--operations", type=int, default=20_000)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.operations < 1 or args.repeats < 1 or args.warmup < 0:
        raise SystemExit("operations and repeats must be positive; warmup cannot be negative")
    workload = HandleWorkload(args.kind, args.operations)
    if args.worker:
        document = worker_result(args.backend, workload, args.warmup, args.repeats)
    else:
        document = {
            "schema_version": 1,
            "benchmark": "phase2-native-handle-design-gate",
            "profile": args.profile,
            "prototype_scope": "benchmark-only; not included in the uringloop runtime ABI",
            "environment": environment_metadata(),
            "results": [
                run_isolated(backend, item, args.warmup, args.repeats)
                for item in workloads(args.profile)
                for backend in BACKENDS
            ],
        }
    serialized = json.dumps(document, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n")
    else:
        print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
