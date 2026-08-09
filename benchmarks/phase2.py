#!/usr/bin/env python3
"""Reproducible Phase 2 event-loop scheduling benchmarks."""

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import resource
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from benchmarks.phase1 import environment_metadata, make_loop, parse_strace_summary, percentile, shutil_which


ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ("native", "cffi", "asyncio", "uvloop")
KINDS = (
    "ready",
    "timer_equal",
    "timer_spread",
    "timer_cancel",
    "mixed",
    "threadsafe_idle",
    "threadsafe_burst",
)
SYSCALL_FILTER = "io_uring_enter,epoll_wait,epoll_pwait,eventfd2,futex,recvfrom,sendto,read,write"
WORKER_TIMEOUT_SECONDS = 180
THREAD_JOIN_TIMEOUT_SECONDS = 1


@dataclass(frozen=True, slots=True)
class SchedulingWorkload:
    kind: str
    operations: int
    producers: int = 1
    cancel_every: int = 0
    spread_ns: int = 0
    lead_ns: int = 0
    idle_settle_ns: int = 0


async def ready_workload(workload: SchedulingWorkload) -> dict[str, list[int]]:
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    latencies = []
    remaining = workload.operations

    def callback(started_ns: int):
        nonlocal remaining
        latencies.append(time.perf_counter_ns() - started_ns)
        remaining -= 1
        if remaining == 0:
            completed.set_result(None)

    for _ in range(workload.operations):
        loop.call_soon(callback, time.perf_counter_ns())
    await completed
    return {"ready": latencies}


async def timer_workload(workload: SchedulingWorkload) -> dict[str, list[int]]:
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    latencies = []
    cancelled = workload.operations // workload.cancel_every if workload.cancel_every else 0
    remaining = workload.operations - cancelled
    if remaining < 1:
        raise ValueError("timer workload must leave at least one live timer")

    def callback(deadline_ns: int):
        nonlocal remaining
        latencies.append(max(0, time.perf_counter_ns() - deadline_ns))
        remaining -= 1
        if remaining == 0:
            completed.set_result(None)

    loop_base = loop.time() + workload.lead_ns / 1_000_000_000
    perf_base = time.perf_counter_ns() + workload.lead_ns
    denominator = max(1, workload.operations - 1)
    for index in range(workload.operations):
        offset_ns = workload.spread_ns * index // denominator
        handle = loop.call_at(
            loop_base + offset_ns / 1_000_000_000,
            callback,
            perf_base + offset_ns,
        )
        if workload.cancel_every and (index + 1) % workload.cancel_every == 0:
            handle.cancel()
    await completed
    return {"timer": latencies}


async def mixed_workload(workload: SchedulingWorkload) -> dict[str, list[int]]:
    loop = asyncio.get_running_loop()
    ready_count = workload.operations // 3
    timer_count = workload.operations // 3
    io_count = workload.operations - ready_count - timer_count
    scheduled_count = ready_count + timer_count
    scheduled_done = loop.create_future()
    latencies = {"ready": [], "timer": [], "io": []}

    def callback(category: str, started_ns: int):
        nonlocal scheduled_count
        latencies[category].append(time.perf_counter_ns() - started_ns)
        scheduled_count -= 1
        if scheduled_count == 0:
            scheduled_done.set_result(None)

    for _ in range(ready_count):
        loop.call_soon(callback, "ready", time.perf_counter_ns())
    for _ in range(timer_count):
        loop.call_at(loop.time(), callback, "timer", time.perf_counter_ns())

    sender, receiver = socket.socketpair()
    sender.setblocking(False)
    receiver.setblocking(False)

    async def run_io():
        try:
            for _ in range(io_count):
                started_ns = time.perf_counter_ns()
                await loop.sock_sendall(sender, b"m")
                data = await loop.sock_recv(receiver, 1)
                if data != b"m":
                    raise RuntimeError("mixed scheduling benchmark payload mismatch")
                latencies["io"].append(time.perf_counter_ns() - started_ns)
        finally:
            sender.close()
            receiver.close()

    if scheduled_count:
        await asyncio.gather(scheduled_done, run_io())
    else:
        await run_io()
    return latencies


async def threadsafe_workload(workload: SchedulingWorkload) -> dict[str, list[int]]:
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    latencies = []
    remaining = workload.operations
    producer_errors: list[BaseException] = []
    acknowledgements = [threading.Event() for _ in range(workload.producers)]

    def callback(started_ns: int, acknowledgement: threading.Event | None):
        nonlocal remaining
        latencies.append(time.perf_counter_ns() - started_ns)
        remaining -= 1
        if acknowledgement is not None:
            acknowledgement.set()
        if remaining == 0:
            completed.set_result(None)

    def producer(count: int, acknowledgement: threading.Event):
        try:
            if workload.kind == "threadsafe_idle" and workload.idle_settle_ns:
                # Give the consumer time to enter its kernel wait before the
                # first measured submission. Subsequent acknowledgements plus
                # the same settle interval establish one wakeup per sample.
                time.sleep(workload.idle_settle_ns / 1_000_000_000)
            for _ in range(count):
                acknowledgement.clear()
                loop.call_soon_threadsafe(
                    callback,
                    time.perf_counter_ns(),
                    acknowledgement if workload.kind == "threadsafe_idle" else None,
                )
                if workload.kind == "threadsafe_idle":
                    if not acknowledgement.wait(timeout=1):
                        raise TimeoutError("idle wakeup callback was not acknowledged")
                    if workload.idle_settle_ns:
                        time.sleep(workload.idle_settle_ns / 1_000_000_000)
        except BaseException as error:
            producer_errors.append(error)

    counts = [workload.operations // workload.producers] * workload.producers
    for index in range(workload.operations % workload.producers):
        counts[index] += 1
    threads = [
        threading.Thread(target=producer, args=(count, acknowledgement), daemon=True)
        for count, acknowledgement in zip(counts, acknowledgements, strict=True)
    ]
    for thread in threads:
        thread.start()
    timeout_error = None
    try:
        await asyncio.wait_for(asyncio.shield(completed), timeout=max(2, workload.operations * 0.02))
    except TimeoutError as error:
        timeout_error = error
    finally:
        join_deadline = time.monotonic() + THREAD_JOIN_TIMEOUT_SECONDS
        for thread in threads:
            thread.join(timeout=max(0, join_deadline - time.monotonic()))
    stuck_threads = sum(thread.is_alive() for thread in threads)
    if stuck_threads:
        raise RuntimeError(f"{stuck_threads} threadsafe benchmark producers did not stop")
    if producer_errors:
        raise ExceptionGroup("threadsafe benchmark producer failures", producer_errors)
    if timeout_error is not None:
        raise TimeoutError("threadsafe benchmark callbacks did not complete within one second") from timeout_error
    return {"wakeup" if workload.kind == "threadsafe_idle" else "enqueue": latencies}


def run_workload(
    loop: asyncio.AbstractEventLoop,
    workload: SchedulingWorkload,
) -> dict[str, list[int]]:
    if workload.kind == "ready":
        coroutine = ready_workload(workload)
    elif workload.kind.startswith("timer_"):
        coroutine = timer_workload(workload)
    elif workload.kind == "mixed":
        coroutine = mixed_workload(workload)
    elif workload.kind.startswith("threadsafe_"):
        coroutine = threadsafe_workload(workload)
    else:
        raise ValueError(f"unknown scheduling workload: {workload.kind}")
    return loop.run_until_complete(coroutine)


def worker_result(
    backend: str,
    workload: SchedulingWorkload,
    warmup: int,
    repeats: int,
) -> dict[str, Any]:
    loop = make_loop(backend)
    asyncio.set_event_loop(loop)
    try:
        for _ in range(warmup):
            run_workload(loop, workload)

        samples = []
        all_latencies: dict[str, list[int]] = {}
        for _ in range(repeats):
            cpu_started = time.process_time_ns()
            wall_started = time.perf_counter_ns()
            latency_groups = run_workload(loop, workload)
            wall_ns = time.perf_counter_ns() - wall_started
            cpu_ns = time.process_time_ns() - cpu_started
            for category, latencies in latency_groups.items():
                all_latencies.setdefault(category, []).extend(latencies)
            samples.append(
                {
                    "wall_seconds": wall_ns / 1_000_000_000,
                    "cpu_seconds": cpu_ns / 1_000_000_000,
                    "operations_per_second": workload.operations * 1_000_000_000 / wall_ns,
                }
            )
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)

    wall_samples = [sample["wall_seconds"] for sample in samples]
    throughput_samples = [sample["operations_per_second"] for sample in samples]
    wall_cv = 0.0
    if len(wall_samples) > 1:
        wall_cv = statistics.stdev(wall_samples) / statistics.mean(wall_samples) * 100
    latency_summary = {
        category: {
            "p50_us": percentile(latencies, 0.50),
            "p99_us": percentile(latencies, 0.99),
            "max_us": max(latencies) / 1_000,
            "samples": len(latencies),
        }
        for category, latencies in sorted(all_latencies.items())
    }
    return {
        "backend": backend,
        "workload": asdict(workload),
        "warmup_runs": warmup,
        "measured_runs": repeats,
        "samples": samples,
        "summary": {
            "wall_seconds_median": statistics.median(wall_samples),
            "wall_time_cv_percent": wall_cv,
            "cpu_seconds_median": statistics.median(sample["cpu_seconds"] for sample in samples),
            "operations_per_second_median": statistics.median(throughput_samples),
            "operations_per_second_min": min(throughput_samples),
            "operations_per_second_max": max(throughput_samples),
            "latencies": latency_summary,
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
    }


def worker_command(
    backend: str,
    workload: SchedulingWorkload,
    warmup: int,
    repeats: int,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "benchmarks.phase2",
        "--worker",
        "--backend",
        backend,
        "--kind",
        workload.kind,
        "--operations",
        str(workload.operations),
        "--producers",
        str(workload.producers),
        "--cancel-every",
        str(workload.cancel_every),
        "--spread-ns",
        str(workload.spread_ns),
        "--lead-ns",
        str(workload.lead_ns),
        "--idle-settle-ns",
        str(workload.idle_settle_ns),
        "--warmup",
        str(warmup),
        "--repeats",
        str(repeats),
    ]


def run_isolated(
    backend: str,
    workload: SchedulingWorkload,
    warmup: int,
    repeats: int,
    syscalls: bool,
) -> dict[str, Any]:
    process = subprocess.run(
        worker_command(backend, workload, warmup, repeats),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=WORKER_TIMEOUT_SECONDS,
    )
    result = json.loads(process.stdout)
    if syscalls:
        if not shutil_which("strace"):
            raise RuntimeError("--syscalls requires strace")
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "strace.txt"
            command = [
                "strace",
                "-f",
                "-qq",
                "-c",
                "-e",
                f"trace={SYSCALL_FILTER}",
                "-o",
                str(summary_path),
                *worker_command(backend, workload, 0, 1),
            ]
            subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
                timeout=WORKER_TIMEOUT_SECONDS,
            )
            result["syscalls"] = parse_strace_summary(summary_path)
    return result


def matrix(profile: str) -> list[SchedulingWorkload]:
    if profile == "smoke":
        return [
            SchedulingWorkload("ready", 1024),
            SchedulingWorkload("timer_equal", 1000),
            SchedulingWorkload("timer_cancel", 1000, cancel_every=2),
            SchedulingWorkload("mixed", 192),
            SchedulingWorkload("threadsafe_idle", 64, idle_settle_ns=1_000_000),
            SchedulingWorkload("threadsafe_burst", 256),
        ]
    workloads = [SchedulingWorkload("ready", operations) for operations in (1, 64, 4096)]
    workloads.extend(SchedulingWorkload("timer_equal", operations) for operations in (1, 1000, 100000))
    workloads.extend(
        SchedulingWorkload("timer_spread", operations, spread_ns=2_000_000_000, lead_ns=250_000_000)
        for operations in (1, 1000, 100000)
    )
    workloads.extend(SchedulingWorkload("timer_cancel", operations, cancel_every=2) for operations in (1000, 100000))
    workloads.append(SchedulingWorkload("mixed", 4096))
    workloads.append(SchedulingWorkload("threadsafe_idle", 100, idle_settle_ns=1_000_000))
    workloads.extend(SchedulingWorkload("threadsafe_burst", 1000, producers=count) for count in (1, 4))
    return workloads


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--backend", choices=BACKENDS, default="native")
    parser.add_argument("--kind", choices=KINDS, default="ready")
    parser.add_argument("--operations", type=int, default=1024)
    parser.add_argument("--producers", type=int, default=1)
    parser.add_argument("--cancel-every", type=int, default=0)
    parser.add_argument("--spread-ns", type=int, default=0)
    parser.add_argument("--lead-ns", type=int, default=0)
    parser.add_argument("--idle-settle-ns", type=int, default=0)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--syscalls", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if (
        min(args.operations, args.producers, args.repeats) < 1
        or min(
            args.cancel_every,
            args.spread_ns,
            args.lead_ns,
            args.idle_settle_ns,
            args.warmup,
        )
        < 0
    ):
        raise SystemExit("operations, producers, and repeats must be positive; other counts cannot be negative")
    workload = SchedulingWorkload(
        args.kind,
        args.operations,
        producers=args.producers,
        cancel_every=args.cancel_every,
        spread_ns=args.spread_ns,
        lead_ns=args.lead_ns,
        idle_settle_ns=args.idle_settle_ns,
    )
    if workload.kind == "threadsafe_idle" and workload.producers != 1:
        raise SystemExit("threadsafe_idle requires exactly one producer")
    if args.worker:
        document = worker_result(args.backend, workload, args.warmup, args.repeats)
    else:
        results = []
        for item in matrix(args.profile):
            for backend in BACKENDS:
                results.append(run_isolated(backend, item, args.warmup, args.repeats, args.syscalls))
        document = {
            "schema_version": 1,
            "benchmark": "phase2-scheduling",
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
