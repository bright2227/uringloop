#!/usr/bin/env python3
"""Isolated Phase 2 bounded run-loop tick benchmarks."""

import argparse
from collections import deque
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
BACKENDS = ("native-core", "python-model")
READY_BUDGET = 4096
TIMER_BUDGET = 4096


class Handle:
    __slots__ = ("_cancelled", "_scheduled", "sequence")

    def __init__(self, sequence):
        self.sequence = sequence
        self._cancelled = False
        self._scheduled = True


@dataclass(frozen=True, slots=True)
class TickWorkload:
    ready: int
    timers: int

    @property
    def operations(self):
        return self.ready + self.timers


def workloads(profile: str):
    sizes = (4096,) if profile == "smoke" else (1, 1000, 100_000)
    return [TickWorkload(size, size) for size in sizes]


def tick_sample(backend: str, workload: TickWorkload) -> dict[str, float | int]:
    ready_handles = [Handle(index) for index in range(workload.ready)]
    timer_handles = [Handle(workload.ready + index) for index in range(workload.timers)]
    started = time.perf_counter_ns()
    cpu_started = time.process_time_ns()
    ticks = 0
    completed = []
    if backend == "native-core":
        state = _uringcore.LoopCore()
        for handle in ready_handles:
            state.enqueue_ready(handle)
        for handle in timer_handles:
            state.schedule_timer(handle, 0.0)
        while state.ready_count or state.timer_count:
            timeout = state.prepare_tick(0.0, 86400.0, False)
            if timeout != 0.0:
                raise RuntimeError("native tick attempted to block with immediate work")
            batch, _, _ = state.finish_tick(0.0, 0.1, TIMER_BUDGET, READY_BUDGET)
            completed.extend(batch)
            ticks += 1
        ready_exhaustions = state.ready_budget_exhaustions
        timer_exhaustions = state.timer_budget_exhaustions
    elif backend == "python-model":
        ready = deque(ready_handles)
        timers = [(0.0, handle.sequence, handle) for handle in timer_handles]
        heapq.heapify(timers)
        ready_exhaustions = 0
        timer_exhaustions = 0
        while ready or timers:
            promoted = 0
            while timers and promoted < TIMER_BUDGET:
                _, _, handle = heapq.heappop(timers)
                handle._scheduled = False
                ready.append(handle)
                promoted += 1
            if timers:
                timer_exhaustions += 1
            for _ in range(min(len(ready), READY_BUDGET)):
                completed.append(ready.popleft())
            if ready:
                ready_exhaustions += 1
            ticks += 1
    else:
        raise ValueError(f"invalid tick backend: {backend}")
    finished = time.perf_counter_ns()
    cpu_finished = time.process_time_ns()
    if len(completed) != workload.operations or len({handle.sequence for handle in completed}) != workload.operations:
        raise RuntimeError("tick benchmark lost or duplicated a handle")
    return {
        "wall_seconds": (finished - started) / 1_000_000_000,
        "cpu_seconds": (cpu_finished - cpu_started) / 1_000_000_000,
        "operations_per_second": workload.operations * 1_000_000_000 / max(1, finished - started),
        "ticks": ticks,
        "ready_budget_exhaustions": ready_exhaustions,
        "timer_budget_exhaustions": timer_exhaustions,
    }


def worker_result(backend: str, workload: TickWorkload, warmup: int, repeats: int) -> dict[str, Any]:
    for _ in range(warmup):
        tick_sample(backend, workload)
    samples = [tick_sample(backend, workload) for _ in range(repeats)]
    wall = [sample["wall_seconds"] for sample in samples]
    return {
        "backend": backend,
        "budgets": {"ready": READY_BUDGET, "timers": TIMER_BUDGET},
        "workload": asdict(workload),
        "warmup_runs": warmup,
        "measured_runs": repeats,
        "samples": samples,
        "summary": {
            "wall_seconds_median": statistics.median(wall),
            "wall_time_cv_percent": statistics.stdev(wall) / statistics.mean(wall) * 100 if len(wall) > 1 else 0.0,
            "cpu_seconds_median": statistics.median(sample["cpu_seconds"] for sample in samples),
            "operations_per_second_median": statistics.median(sample["operations_per_second"] for sample in samples),
            "ticks_median": statistics.median(sample["ticks"] for sample in samples),
            "ready_budget_exhaustions_median": statistics.median(sample["ready_budget_exhaustions"] for sample in samples),
            "timer_budget_exhaustions_median": statistics.median(sample["timer_budget_exhaustions"] for sample in samples),
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
    }


def run_isolated(backend, workload, warmup, repeats):
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.run_loop_tick",
            "--worker",
            "--backend",
            backend,
            "--ready",
            str(workload.ready),
            "--timers",
            str(workload.timers),
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
    parser.add_argument("--backend", choices=BACKENDS, default="native-core")
    parser.add_argument("--ready", type=int, default=4096)
    parser.add_argument("--timers", type=int, default=4096)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.ready, args.timers, args.repeats) < 1 or args.warmup < 0:
        raise SystemExit("ready, timers, and repeats must be positive; warmup cannot be negative")
    workload = TickWorkload(args.ready, args.timers)
    if args.worker:
        document = worker_result(args.backend, workload, args.warmup, args.repeats)
    else:
        document = {
            "schema_version": 1,
            "benchmark": "phase2-native-run-loop-tick",
            "profile": args.profile,
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
