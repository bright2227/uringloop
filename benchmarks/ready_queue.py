#!/usr/bin/env python3
"""Isolated Phase 2 native-ready-queue benchmarks."""

import argparse
import asyncio
from collections import deque
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time
from typing import Any

from benchmarks.phase1 import environment_metadata, percentile
from benchmarks.phase2 import WORKER_TIMEOUT_SECONDS
from uringloop import _uringcore
from uringloop.loop import _NativeReadyQueue


ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ("native-core", "python-deque", "native-loop", "python-loop")


class _NullSelector:
    def select(self, timeout=None):
        return []


class _ReadyQueueBenchmarkLoop(asyncio.BaseEventLoop):
    """Minimal event loop that isolates BaseEventLoop ready dispatch.

    A real proactor would add ring setup and completion-wait noise to a
    benchmark whose only variable is ready-queue storage.  The production
    adapter is still exercised by BaseEventLoop's actual scheduling and
    ``_run_once`` implementation.
    """

    def __init__(self, *, native: bool):
        super().__init__()
        self._selector = _NullSelector()
        if native:
            self._ready = _NativeReadyQueue()

    def _process_events(self, event_list):
        if event_list:
            raise RuntimeError("ready-queue benchmark received unexpected I/O events")

    def close(self):
        ready = self._ready
        super().close()
        if isinstance(ready, _NativeReadyQueue):
            ready.close()


@dataclass(frozen=True, slots=True)
class ReadyQueueWorkload:
    kind: str
    operations: int


def workloads(profile: str) -> list[ReadyQueueWorkload]:
    operations = (4096,) if profile == "smoke" else (1, 64, 4096, 100_000)
    return [ReadyQueueWorkload(kind, count) for count in operations for kind in ("queue", "loop")]


def backends_for(workload: ReadyQueueWorkload) -> tuple[str, str]:
    if workload.kind == "queue":
        return ("native-core", "python-deque")
    if workload.kind == "loop":
        return ("native-loop", "python-loop")
    raise ValueError(f"unknown ready-queue workload: {workload.kind}")


def queue_sample(backend: str, operations: int) -> dict[str, float]:
    handles = [object() for _ in range(operations)]
    if backend == "native-core":
        ready = _uringcore.LoopCore()
        enqueue = ready.enqueue_ready

        def drain():
            return ready.take_ready_batch(operations)
    elif backend == "python-deque":
        ready = deque()
        enqueue = ready.append

        def drain():
            batch = list(ready)
            ready.clear()
            return batch

    else:
        raise ValueError(f"invalid direct queue backend: {backend}")

    wall_started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    for handle in handles:
        enqueue(handle)
    enqueued_ns = time.perf_counter_ns()
    batch = drain()
    wall_finished = time.perf_counter_ns()
    cpu_finished = time.process_time_ns()
    if batch != handles:
        raise RuntimeError("ready queue benchmark changed FIFO ordering")
    enqueue_ns = enqueued_ns - wall_started
    drain_ns = wall_finished - enqueued_ns
    return {
        "wall_seconds": (wall_finished - wall_started) / 1_000_000_000,
        "cpu_seconds": (cpu_finished - cpu_started) / 1_000_000_000,
        "enqueue_seconds": enqueue_ns / 1_000_000_000,
        "drain_seconds": drain_ns / 1_000_000_000,
        "enqueue_operations_per_second": operations * 1_000_000_000 / max(1, enqueue_ns),
        "drain_operations_per_second": operations * 1_000_000_000 / max(1, drain_ns),
    }


async def run_loop_callbacks(operations: int) -> list[int]:
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    remaining = operations
    latencies = []

    def callback(started_ns: int):
        nonlocal remaining
        latencies.append(time.perf_counter_ns() - started_ns)
        remaining -= 1
        if remaining == 0:
            completed.set_result(None)

    for _ in range(operations):
        loop.call_soon(callback, time.perf_counter_ns())
    await completed
    return latencies


def loop_samples(backend: str, operations: int, warmup: int, repeats: int) -> tuple[list[dict[str, float]], list[int]]:
    loop = _ReadyQueueBenchmarkLoop(native=backend == "native-loop")
    asyncio.set_event_loop(loop)
    samples = []
    all_latencies = []
    try:
        for _ in range(warmup):
            loop.run_until_complete(run_loop_callbacks(operations))
        for _ in range(repeats):
            wall_started = time.perf_counter_ns()
            cpu_started = time.process_time_ns()
            latencies = loop.run_until_complete(run_loop_callbacks(operations))
            wall_ns = time.perf_counter_ns() - wall_started
            cpu_ns = time.process_time_ns() - cpu_started
            all_latencies.extend(latencies)
            samples.append(
                {
                    "wall_seconds": wall_ns / 1_000_000_000,
                    "cpu_seconds": cpu_ns / 1_000_000_000,
                    "operations_per_second": operations * 1_000_000_000 / wall_ns,
                }
            )
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)
    return samples, all_latencies


def worker_result(backend: str, workload: ReadyQueueWorkload, warmup: int, repeats: int) -> dict[str, Any]:
    if backend not in backends_for(workload):
        raise ValueError(f"backend {backend} does not support {workload.kind}")
    all_latencies: list[int] = []
    if workload.kind == "queue":
        for _ in range(warmup):
            queue_sample(backend, workload.operations)
        samples = [queue_sample(backend, workload.operations) for _ in range(repeats)]
    else:
        samples, all_latencies = loop_samples(backend, workload.operations, warmup, repeats)

    wall_samples = [sample["wall_seconds"] for sample in samples]
    throughput_samples = [
        sample.get("operations_per_second", workload.operations / sample["wall_seconds"]) for sample in samples
    ]
    summary: dict[str, float | int] = {
        "wall_seconds_median": statistics.median(wall_samples),
        "wall_time_cv_percent": statistics.stdev(wall_samples) / statistics.mean(wall_samples) * 100
        if len(wall_samples) > 1
        else 0.0,
        "cpu_seconds_median": statistics.median(sample["cpu_seconds"] for sample in samples),
        "operations_per_second_median": statistics.median(throughput_samples),
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    if workload.kind == "queue":
        summary.update(
            {
                "enqueue_operations_per_second_median": statistics.median(
                    sample["enqueue_operations_per_second"] for sample in samples
                ),
                "drain_operations_per_second_median": statistics.median(
                    sample["drain_operations_per_second"] for sample in samples
                ),
            }
        )
    else:
        summary.update(
            {
                "latency_p50_us": percentile(all_latencies, 0.50),
                "latency_p99_us": percentile(all_latencies, 0.99),
                "latency_samples": len(all_latencies),
            }
        )
    return {
        "backend": backend,
        "workload": asdict(workload),
        "warmup_runs": warmup,
        "measured_runs": repeats,
        "samples": samples,
        "summary": summary,
    }


def worker_command(backend: str, workload: ReadyQueueWorkload, warmup: int, repeats: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "benchmarks.ready_queue",
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
    ]


def run_isolated(backend: str, workload: ReadyQueueWorkload, warmup: int, repeats: int) -> dict[str, Any]:
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
    parser.add_argument("--kind", choices=("queue", "loop"), default="queue")
    parser.add_argument("--operations", type=int, default=4096)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if min(args.operations, args.repeats) < 1 or args.warmup < 0:
        raise SystemExit("operations and repeats must be positive; warmup cannot be negative")
    workload = ReadyQueueWorkload(args.kind, args.operations)
    if args.worker:
        document = worker_result(args.backend, workload, args.warmup, args.repeats)
    else:
        results = [
            run_isolated(backend, workload, args.warmup, args.repeats)
            for workload in workloads(args.profile)
            for backend in backends_for(workload)
        ]
        document = {
            "schema_version": 1,
            "benchmark": "phase2-native-ready-queue",
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
