#!/usr/bin/env python3
"""Isolated Phase 2 native-timer-heap benchmarks."""

import argparse
from dataclasses import asdict, dataclass
import heapq
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time
from typing import Any

from benchmarks.phase1 import environment_metadata
from benchmarks.phase2 import WORKER_TIMEOUT_SECONDS
from uringloop import _uringcore


ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ("native-core", "python-heap")


class Timer:
    __slots__ = ("_cancelled", "_scheduled", "sequence")

    def __init__(self, sequence: int):
        self.sequence = sequence
        self._cancelled = False
        self._scheduled = True


@dataclass(frozen=True, slots=True)
class TimerHeapWorkload:
    operations: int
    cancel_every: int = 2


def workloads(profile: str) -> list[TimerHeapWorkload]:
    operations = (4096,) if profile == "smoke" else (1, 1000, 100_000)
    return [TimerHeapWorkload(count) for count in operations]


def timer_sample(backend: str, workload: TimerHeapWorkload) -> dict[str, float]:
    operations = workload.operations
    timers = [Timer(index) for index in range(operations)]
    deadlines = [float((index * 17) % max(1, operations)) for index in range(operations)]
    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()

    if backend == "native-core":
        heap = _uringcore.LoopCore()
        for timer, deadline in zip(timers, deadlines, strict=True):
            heap.schedule_timer(timer, deadline)
    elif backend == "python-heap":
        heap = []
        for timer, deadline in zip(timers, deadlines, strict=True):
            heapq.heappush(heap, (deadline, timer.sequence, timer))
    else:
        raise ValueError(f"invalid timer heap backend: {backend}")
    inserted_ns = time.perf_counter_ns()

    for index in range(0, operations, workload.cancel_every):
        timers[index]._cancelled = True
    cancelled_ns = time.perf_counter_ns()

    if backend == "native-core":
        next_deadline = heap.next_deadline()
        while heap.timer_count:
            heap.promote_due(float(operations + 1), 0.0, operations)
        promoted = heap.take_ready_batch(operations)
    else:
        while heap and heap[0][2]._cancelled:
            heapq.heappop(heap)
        next_deadline = heap[0][0] if heap else None
        promoted = []
        while heap:
            _, _, timer = heapq.heappop(heap)
            if not timer._cancelled:
                timer._scheduled = False
                promoted.append(timer)
    finished_ns = time.perf_counter_ns()
    cpu_finished = time.process_time_ns()
    expected = operations - len(range(0, operations, workload.cancel_every))
    if len(promoted) != expected or (expected and next_deadline is None):
        raise RuntimeError("timer heap benchmark changed cancellation or expiry semantics")
    return {
        "wall_seconds": (finished_ns - wall_started) / 1_000_000_000,
        "cpu_seconds": (cpu_finished - cpu_started) / 1_000_000_000,
        "insert_seconds": (inserted_ns - wall_started) / 1_000_000_000,
        "cancel_seconds": (cancelled_ns - inserted_ns) / 1_000_000_000,
        "expire_seconds": (finished_ns - cancelled_ns) / 1_000_000_000,
        "operations_per_second": operations * 1_000_000_000 / max(1, finished_ns - wall_started),
    }


def worker_result(backend: str, workload: TimerHeapWorkload, warmup: int, repeats: int) -> dict[str, Any]:
    if backend not in BACKENDS:
        raise ValueError(f"invalid timer heap backend: {backend}")
    for _ in range(warmup):
        timer_sample(backend, workload)
    samples = [timer_sample(backend, workload) for _ in range(repeats)]
    wall_samples = [sample["wall_seconds"] for sample in samples]
    summary = {
        "wall_seconds_median": statistics.median(wall_samples),
        "wall_time_cv_percent": statistics.stdev(wall_samples) / statistics.mean(wall_samples) * 100
        if len(wall_samples) > 1
        else 0.0,
        "cpu_seconds_median": statistics.median(sample["cpu_seconds"] for sample in samples),
        "insert_seconds_median": statistics.median(sample["insert_seconds"] for sample in samples),
        "cancel_seconds_median": statistics.median(sample["cancel_seconds"] for sample in samples),
        "expire_seconds_median": statistics.median(sample["expire_seconds"] for sample in samples),
        "operations_per_second_median": statistics.median(sample["operations_per_second"] for sample in samples),
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


def worker_command(backend: str, workload: TimerHeapWorkload, warmup: int, repeats: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "benchmarks.timer_heap",
        "--worker",
        "--backend",
        backend,
        "--operations",
        str(workload.operations),
        "--cancel-every",
        str(workload.cancel_every),
        "--warmup",
        str(warmup),
        "--repeats",
        str(repeats),
    ]


def run_isolated(backend: str, workload: TimerHeapWorkload, warmup: int, repeats: int) -> dict[str, Any]:
    process = subprocess.run(
        worker_command(backend, workload, warmup, repeats),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=WORKER_TIMEOUT_SECONDS,
    )
    return json.loads(process.stdout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--backend", choices=BACKENDS, default="native-core")
    parser.add_argument("--operations", type=int, default=4096)
    parser.add_argument("--cancel-every", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if min(args.operations, args.cancel_every, args.repeats) < 1 or args.warmup < 0:
        raise SystemExit("operations, cancel-every, and repeats must be positive; warmup cannot be negative")
    workload = TimerHeapWorkload(args.operations, args.cancel_every)
    if args.worker:
        document = worker_result(args.backend, workload, args.warmup, args.repeats)
    else:
        results = [
            run_isolated(backend, workload, args.warmup, args.repeats)
            for workload in workloads(args.profile)
            for backend in BACKENDS
        ]
        document = {
            "schema_version": 1,
            "benchmark": "phase2-native-timer-heap",
            "profile": args.profile,
            "environment": environment_metadata(),
            "results": results,
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
