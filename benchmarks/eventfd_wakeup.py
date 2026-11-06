#!/usr/bin/env python3
"""Isolated Phase 2 cross-thread wakeup primitive benchmarks."""

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import resource
import select
import socket
import statistics
import subprocess
import sys
import threading
import time
from typing import Any

from benchmarks.phase1 import environment_metadata, percentile
from benchmarks.phase2 import WORKER_TIMEOUT_SECONDS
from uringloop import _uringcore


ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ("eventfd", "socketpair")


class SocketWakeup:
    def __init__(self):
        self.reader, self.writer = socket.socketpair()
        self.reader.setblocking(False)
        self.writer.setblocking(False)
        self.write_count = 0
        self.drain_count = 0

    def fileno(self):
        return self.reader.fileno()

    def notify(self):
        try:
            self.writer.send(b"\0")
        except BlockingIOError:
            return False
        self.write_count += 1
        return True

    def drain(self):
        total = 0
        while True:
            try:
                chunk = self.reader.recv(65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            total += len(chunk)
            self.drain_count += 1
        return total

    def close(self):
        self.reader.close()
        self.writer.close()


@dataclass(frozen=True, slots=True)
class WakeupWorkload:
    idle_operations: int
    burst_operations: int
    producers: int


def workloads(profile: str) -> list[WakeupWorkload]:
    if profile == "smoke":
        return [WakeupWorkload(64, 4096, 4)]
    return [WakeupWorkload(1000, 100_000, producers) for producers in (1, 4, 8)]


def wakeup_sample(backend: str, workload: WakeupWorkload) -> dict[str, Any]:
    wakeup = _uringcore.EventFD() if backend == "eventfd" else SocketWakeup()
    poller = select.poll()
    poller.register(wakeup.fileno(), select.POLLIN)
    publish = threading.Event()
    drained = threading.Event()
    timestamps = [0] * workload.idle_operations

    def idle_producer():
        for index in range(workload.idle_operations):
            publish.wait()
            publish.clear()
            timestamps[index] = time.perf_counter_ns()
            wakeup.notify()
            drained.wait()
            drained.clear()

    started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    producer = threading.Thread(target=idle_producer)
    producer.start()
    latencies = []
    for index in range(workload.idle_operations):
        publish.set()
        if not poller.poll(5000):
            raise TimeoutError("idle wakeup benchmark timed out")
        latencies.append(time.perf_counter_ns() - timestamps[index])
        wakeup.drain()
        drained.set()
    producer.join(timeout=5)
    if producer.is_alive():
        raise TimeoutError("idle wakeup producer did not stop")
    idle_writes = wakeup.write_count

    barrier = threading.Barrier(workload.producers + 1)

    def burst_producer(count):
        barrier.wait()
        for _ in range(count):
            wakeup.notify()

    counts = [workload.burst_operations // workload.producers] * workload.producers
    for index in range(workload.burst_operations % workload.producers):
        counts[index] += 1
    producers = [threading.Thread(target=burst_producer, args=(count,)) for count in counts]
    for thread in producers:
        thread.start()
    barrier.wait()
    for thread in producers:
        thread.join(timeout=5)
    if any(thread.is_alive() for thread in producers):
        raise TimeoutError("burst wakeup producer did not stop")
    if not poller.poll(5000):
        raise TimeoutError("burst wakeup benchmark timed out")
    wakeup.drain()
    finished = time.perf_counter_ns()
    cpu_finished = time.process_time_ns()
    burst_writes = wakeup.write_count - idle_writes
    result = {
        "wall_seconds": (finished - started) / 1_000_000_000,
        "cpu_seconds": (cpu_finished - cpu_started) / 1_000_000_000,
        "idle_latency_p50_us": percentile(latencies, 0.50),
        "idle_latency_p99_us": percentile(latencies, 0.99),
        "idle_writes": idle_writes,
        "burst_writes": burst_writes,
        "drain_reads": wakeup.drain_count,
    }
    wakeup.close()
    return result


def worker_result(backend: str, workload: WakeupWorkload, warmup: int, repeats: int) -> dict[str, Any]:
    if backend not in BACKENDS:
        raise ValueError(f"invalid wakeup backend: {backend}")
    for _ in range(warmup):
        wakeup_sample(backend, workload)
    samples = [wakeup_sample(backend, workload) for _ in range(repeats)]
    wall = [sample["wall_seconds"] for sample in samples]
    return {
        "backend": backend,
        "workload": asdict(workload),
        "warmup_runs": warmup,
        "measured_runs": repeats,
        "samples": samples,
        "summary": {
            "wall_seconds_median": statistics.median(wall),
            "wall_time_cv_percent": statistics.stdev(wall) / statistics.mean(wall) * 100 if len(wall) > 1 else 0.0,
            "cpu_seconds_median": statistics.median(sample["cpu_seconds"] for sample in samples),
            "idle_latency_p50_us_median": statistics.median(sample["idle_latency_p50_us"] for sample in samples),
            "idle_latency_p99_us_median": statistics.median(sample["idle_latency_p99_us"] for sample in samples),
            "idle_writes_median": statistics.median(sample["idle_writes"] for sample in samples),
            "burst_writes_median": statistics.median(sample["burst_writes"] for sample in samples),
            "drain_reads_median": statistics.median(sample["drain_reads"] for sample in samples),
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
    }


def run_isolated(backend: str, workload: WakeupWorkload, warmup: int, repeats: int) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "benchmarks.eventfd_wakeup",
        "--worker",
        "--backend",
        backend,
        "--idle-operations",
        str(workload.idle_operations),
        "--burst-operations",
        str(workload.burst_operations),
        "--producers",
        str(workload.producers),
        "--warmup",
        str(warmup),
        "--repeats",
        str(repeats),
    ]
    process = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=WORKER_TIMEOUT_SECONDS,
    )
    return json.loads(process.stdout)


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--backend", choices=BACKENDS)
    parser.add_argument("--idle-operations", type=int, default=64)
    parser.add_argument("--burst-operations", type=int, default=4096)
    parser.add_argument("--producers", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if min(args.idle_operations, args.burst_operations, args.producers, args.repeats) < 1 or args.warmup < 0:
        raise SystemExit("operation counts, producers, and repeats must be positive; warmup cannot be negative")
    workload = WakeupWorkload(args.idle_operations, args.burst_operations, args.producers)
    if args.worker:
        document = worker_result(args.backend or "eventfd", workload, args.warmup, args.repeats)
    else:
        selected_backends = (args.backend,) if args.backend else BACKENDS
        document = {
            "schema_version": 1,
            "benchmark": "phase2-eventfd-wakeup",
            "profile": args.profile,
            "environment": environment_metadata(),
            "results": [
                run_isolated(backend, item, args.warmup, args.repeats)
                for item in workloads(args.profile)
                for backend in selected_backends
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
