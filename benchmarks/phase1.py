#!/usr/bin/env python3
"""Reproducible Phase 1 benchmarks for uringloop.

The normal entry point runs an isolated backend/configuration matrix and emits
one JSON document. ``--worker`` runs exactly one sample and is intentionally
public so the runner can wrap it in strace without measuring orchestration.
"""

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import platform
import resource
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

from uringloop.loop import IoUringProactorEventLoop
from uringloop.proactor import IoUringProactor, _CffiIoUringProactor


ROOT = Path(__file__).resolve().parents[1]
SYSCALL_FILTER = "io_uring_enter,epoll_wait,epoll_pwait,sendto,recvfrom,sendmsg,recvmsg,read,write"


@dataclass(frozen=True, slots=True)
class Workload:
    api: str
    connections: int
    message_size: int
    rounds: int

    @property
    def operations(self) -> int:
        return self.rounds if self.api == "file" else self.connections * self.rounds


def percentile(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index] / 1_000


async def receive_exactly(loop: asyncio.AbstractEventLoop, sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = await loop.sock_recv(sock, size - len(chunks))
        if not chunk:
            raise EOFError(f"socket closed after {len(chunks)} of {size} bytes")
        chunks.extend(chunk)
    return bytes(chunks)


async def socket_workload(workload: Workload) -> list[int]:
    loop = asyncio.get_running_loop()
    payload = b"u" * workload.message_size
    pairs = [socket.socketpair() for _ in range(workload.connections)]
    for client, server in pairs:
        client.setblocking(False)
        server.setblocking(False)
        if workload.message_size >= 64 * 1024:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16 * 1024)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16 * 1024)

    async def echo(server: socket.socket):
        for _ in range(workload.rounds):
            data = await receive_exactly(loop, server, len(payload))
            await loop.sock_sendall(server, data)

    async def client(client_socket: socket.socket) -> list[int]:
        latencies = []
        for _ in range(workload.rounds):
            started = time.perf_counter_ns()
            await loop.sock_sendall(client_socket, payload)
            response = await receive_exactly(loop, client_socket, len(payload))
            if response != payload:
                raise RuntimeError("socket echo payload mismatch")
            latencies.append(time.perf_counter_ns() - started)
        return latencies

    try:
        echo_tasks = [asyncio.create_task(echo(server)) for _, server in pairs]
        samples = await asyncio.gather(*(client(client_socket) for client_socket, _ in pairs))
        await asyncio.gather(*echo_tasks)
        return [sample for connection in samples for sample in connection]
    finally:
        for pair in pairs:
            for endpoint in pair:
                endpoint.close()


async def streams_workload(workload: Workload) -> list[int]:
    payload = b"s" * workload.message_size

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            for _ in range(workload.rounds):
                data = await reader.readexactly(len(payload))
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    address = server.sockets[0].getsockname()

    async def client() -> list[int]:
        reader, writer = await asyncio.open_connection(*address[:2])
        latencies = []
        try:
            for _ in range(workload.rounds):
                started = time.perf_counter_ns()
                writer.write(payload)
                await writer.drain()
                response = await reader.readexactly(len(payload))
                if response != payload:
                    raise RuntimeError("streams echo payload mismatch")
                latencies.append(time.perf_counter_ns() - started)
            return latencies
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        samples = await asyncio.gather(*(client() for _ in range(workload.connections)))
        return [sample for connection in samples for sample in connection]
    finally:
        server.close()
        await server.wait_closed()


async def file_workload(workload: Workload, backend: str) -> list[int]:
    loop = asyncio.get_running_loop()
    payload = b"f" * workload.message_size * workload.rounds
    latencies = []
    with tempfile.TemporaryFile() as file:
        file.write(payload)
        file.flush()
        file.seek(0)
        for _ in range(workload.rounds):
            started = time.perf_counter_ns()
            if backend in {"asyncio", "uvloop"}:
                data = await asyncio.to_thread(file.read, workload.message_size)
            else:
                data = await loop._proactor.recv(file, workload.message_size)  # type: ignore[attr-defined]
            if len(data) != workload.message_size:
                raise RuntimeError("file benchmark produced a short read")
            latencies.append(time.perf_counter_ns() - started)
    return latencies


def make_loop(backend: str) -> asyncio.AbstractEventLoop:
    if backend == "native":
        return IoUringProactorEventLoop(IoUringProactor())
    if backend == "cffi":
        return IoUringProactorEventLoop(_CffiIoUringProactor())
    if backend == "asyncio":
        return asyncio.new_event_loop()
    if backend == "uvloop":
        try:
            import uvloop
        except ImportError as error:
            raise RuntimeError("uvloop backend requires the benchmark dependency group") from error
        return uvloop.new_event_loop()
    raise ValueError(f"unknown backend: {backend}")


def run_workload(loop: asyncio.AbstractEventLoop, backend: str, workload: Workload) -> list[int]:
    if workload.api == "socket":
        coroutine = socket_workload(workload)
    elif workload.api == "streams":
        coroutine = streams_workload(workload)
    elif workload.api == "file":
        coroutine = file_workload(workload, backend)
    else:
        raise ValueError(f"unknown API: {workload.api}")
    return loop.run_until_complete(coroutine)


def worker_result(backend: str, workload: Workload, warmup: int, repeats: int) -> dict[str, Any]:
    loop = make_loop(backend)
    asyncio.set_event_loop(loop)
    try:
        for _ in range(warmup):
            run_workload(loop, backend, workload)

        samples = []
        all_latencies = []
        for _ in range(repeats):
            cpu_started = time.process_time_ns()
            wall_started = time.perf_counter_ns()
            latencies = run_workload(loop, backend, workload)
            wall_ns = time.perf_counter_ns() - wall_started
            cpu_ns = time.process_time_ns() - cpu_started
            all_latencies.extend(latencies)
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
            "latency_p50_us": percentile(all_latencies, 0.50),
            "latency_p99_us": percentile(all_latencies, 0.99),
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
    }


def parse_strace_summary(path: Path) -> dict[str, Any]:
    raw = path.read_text()
    calls: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0][0:1].isdigit():
            continue
        name = parts[-1]
        if name == "total":
            continue
        try:
            calls[name] = int(parts[3])
        except (ValueError, IndexError):
            continue
    return {"calls": calls, "total_calls": sum(calls.values()), "raw_summary": raw}


def worker_command(backend: str, workload: Workload, warmup: int, repeats: int) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--backend",
        backend,
        "--api",
        workload.api,
        "--connections",
        str(workload.connections),
        "--message-size",
        str(workload.message_size),
        "--rounds",
        str(workload.rounds),
        "--warmup",
        str(warmup),
        "--repeats",
        str(repeats),
    ]


def run_isolated(backend: str, workload: Workload, warmup: int, repeats: int, syscalls: bool) -> dict[str, Any]:
    command = worker_command(backend, workload, warmup, repeats)
    process = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
    result = json.loads(process.stdout)
    if syscalls:
        if not shutil_which("strace"):
            raise RuntimeError("--syscalls requires strace")
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "strace.txt"
            traced = [
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
            subprocess.run(traced, cwd=ROOT, text=True, capture_output=True, check=True)
            result["syscalls"] = parse_strace_summary(summary_path)
    return result


def shutil_which(command: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def matrix(profile: str) -> list[Workload]:
    if profile == "smoke":
        return [
            Workload("socket", 1, 64, 50),
            Workload("streams", 1, 4096, 25),
            Workload("file", 1, 4096, 50),
        ]
    workloads = []
    for api in ("socket", "streams"):
        for connections in (1, 64, 1000):
            for message_size in (64, 4096, 262144):
                rounds = 100 if connections == 1 else 20 if connections == 64 else 5
                workloads.append(Workload(api, connections, message_size, rounds))
    workloads.extend(Workload("file", 1, size, 200) for size in (4096, 262144))
    return workloads


def environment_metadata() -> dict[str, Any]:
    return {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
        ).stdout.strip(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--backend", choices=("native", "cffi", "asyncio", "uvloop"), default="native")
    parser.add_argument("--api", choices=("socket", "streams", "file"), default="socket")
    parser.add_argument("--connections", type=int, default=1)
    parser.add_argument("--message-size", type=int, default=64)
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--syscalls", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if min(args.connections, args.message_size, args.rounds, args.repeats) < 1 or args.warmup < 0:
        raise SystemExit("connections, message-size, rounds, and repeats must be positive; warmup cannot be negative")

    if args.worker:
        document = worker_result(
            args.backend,
            Workload(args.api, args.connections, args.message_size, args.rounds),
            args.warmup,
            args.repeats,
        )
    else:
        results = []
        for workload in matrix(args.profile):
            for backend in ("native", "cffi", "asyncio", "uvloop"):
                results.append(run_isolated(backend, workload, args.warmup, args.repeats, args.syscalls))
        document = {
            "schema_version": 1,
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
