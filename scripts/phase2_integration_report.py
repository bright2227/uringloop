#!/usr/bin/env python3
"""Build an auditable Phase 2 integration evidence index."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from benchmarks.phase1 import environment_metadata


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks/results"
ARTIFACTS = {
    "contracts": "phase2-contracts-2026-08-10-wsl2.json",
    "ready_queue": "phase2-ready-queue-2026-08-10-wsl2.json",
    "timer_heap": "phase2-timer-heap-2026-08-10-wsl2.json",
    "eventfd": "phase2-eventfd-wakeup-2026-08-10-wsl2.json",
    "native_tick": "phase2-native-tick-2026-08-10-wsl2.json",
    "native_handles": "phase2-native-handles-2026-08-10-wsl2.json",
}
REVIEWED_BRANCH_TIPS = {
    "phase2-loop-contracts": "215059a",
    "phase2-native-ready-queue": "76a1bcc",
    "phase2-native-timer-heap": "1a3cb22",
    "phase2-eventfd-wakeup": "1a9e5eb",
    "phase2-native-run-loop": "396c092",
    "phase2-native-handles": "a383f76",
}


def load_artifacts(directory: Path = RESULTS) -> dict[str, dict[str, Any]]:
    documents = {}
    for name, filename in ARTIFACTS.items():
        path = directory / filename
        document = json.loads(path.read_text())
        if document.get("schema_version") != 1 or not document.get("results"):
            raise ValueError(f"invalid or empty Phase 2 artifact: {path}")
        documents[name] = document
    return documents


def result_for(document: dict[str, Any], backend: str, *, kind: str | None = None) -> dict[str, Any]:
    matches = [
        result
        for result in document["results"]
        if result["backend"] == backend and (kind is None or result["workload"].get("kind") == kind)
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one {backend}/{kind} result, found {len(matches)}")
    return matches[0]


def percentage_change(value: float, reference: float) -> float:
    return (value / reference - 1.0) * 100.0


def build_report(directory: Path = RESULTS) -> dict[str, Any]:
    documents = load_artifacts(directory)
    ready_native = result_for(documents["ready_queue"], "native-core", kind="queue")["summary"]
    ready_python = result_for(documents["ready_queue"], "python-deque", kind="queue")["summary"]
    loop_native = result_for(documents["ready_queue"], "native-loop", kind="loop")["summary"]
    loop_python = result_for(documents["ready_queue"], "python-loop", kind="loop")["summary"]
    timer_native = result_for(documents["timer_heap"], "native-core")["summary"]
    timer_python = result_for(documents["timer_heap"], "python-heap")["summary"]
    tick_native = result_for(documents["native_tick"], "native-core")["summary"]
    tick_python = result_for(documents["native_tick"], "python-model")["summary"]
    handle_stock = result_for(documents["native_handles"], "stock-python", kind="handle")["summary"]
    handle_native = result_for(documents["native_handles"], "full-native-prototype", kind="handle")["summary"]
    timer_handle_stock = result_for(documents["native_handles"], "stock-python", kind="timer")["summary"]
    timer_handle_native = result_for(documents["native_handles"], "full-native-prototype", kind="timer")["summary"]
    contracts = documents["contracts"]["results"]
    eventfd = result_for(documents["eventfd"], "eventfd")["summary"]

    sources = []
    for name, filename in ARTIFACTS.items():
        payload = (directory / filename).read_bytes()
        document = documents[name]
        sources.append(
            {
                "name": name,
                "path": f"benchmarks/results/{filename}",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "harness_commit": document["environment"]["git_commit"],
                "result_count": len(document["results"]),
            }
        )

    return {
        "schema_version": 1,
        "report": "phase2-integration-evidence-index",
        "environment": environment_metadata(),
        "reviewed_branch_tips": REVIEWED_BRANCH_TIPS,
        "source_artifacts": sources,
        "contract_baseline": {
            "result_count": len(contracts),
            "backend_count": len({result["backend"] for result in contracts}),
            "workload_count": len({result["workload"]["kind"] for result in contracts}),
            "results_with_syscalls": sum("syscalls" in result for result in contracts),
        },
        "derived_metrics": {
            "ready_queue": {
                "direct_throughput_change_percent": percentage_change(
                    ready_native["operations_per_second_median"],
                    ready_python["operations_per_second_median"],
                ),
                "loop_throughput_change_percent": percentage_change(
                    loop_native["operations_per_second_median"],
                    loop_python["operations_per_second_median"],
                ),
                "native_loop_cv_percent": loop_native["wall_time_cv_percent"],
                "python_loop_cv_percent": loop_python["wall_time_cv_percent"],
            },
            "timer_heap": {
                "throughput_change_percent": percentage_change(
                    timer_native["operations_per_second_median"],
                    timer_python["operations_per_second_median"],
                ),
                "native_cv_percent": timer_native["wall_time_cv_percent"],
                "python_cv_percent": timer_python["wall_time_cv_percent"],
            },
            "native_tick": {
                "throughput_change_percent": percentage_change(
                    tick_native["operations_per_second_median"],
                    tick_python["operations_per_second_median"],
                ),
                "native_cv_percent": tick_native["wall_time_cv_percent"],
                "python_cv_percent": tick_python["wall_time_cv_percent"],
            },
            "eventfd": {
                "comparison_available": False,
                "idle_latency_p50_us": eventfd["idle_latency_p50_us_median"],
                "idle_latency_p99_us": eventfd["idle_latency_p99_us_median"],
                "burst_writes": eventfd["burst_writes_median"],
            },
            "native_handles": {
                "handle_wall_time_change_percent": percentage_change(
                    handle_native["wall_seconds_median"],
                    handle_stock["wall_seconds_median"],
                ),
                "timer_wall_time_change_percent": percentage_change(
                    timer_handle_native["wall_seconds_median"],
                    timer_handle_stock["wall_seconds_median"],
                ),
            },
        },
        "integrated_matrix": {
            "local_status": "not_run",
            "reason": "managed sandbox denies io_uring_setup and network socket creation with EPERM",
            "ci_backends": ["native-phase2", "native", "cffi-phase2", "cffi", "asyncio", "uvloop"],
        },
        "decision": {
            "default_loop_core": "phase1",
            "phase2_selector": "loop_core='phase2'",
            "proceed_to_phase3": False,
            "reason": "isolated native wins are not yet supported by a controlled integrated matrix without material regressions",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=RESULTS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    document = build_report(args.results)
    serialized = json.dumps(document, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n")
    else:
        print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
