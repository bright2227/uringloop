#!/usr/bin/env python3
"""Verified Protocol/BufferedProtocol transport benchmark scaffolding.

Run as ``python -m benchmarks.phase3``. Backend names select the ring and
explicit Phase 2 opt-in from phase1. ``--transports native`` separately selects
experimental one-shot stream transports with native ring backends only.
Actual transport classes are recorded from established connections.
Timing includes connection setup, payload verification, echo, and cleanup.
"""

import argparse
import asyncio
from dataclasses import asdict, dataclass
import errno
import hashlib
import json
from pathlib import Path
import resource
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

from benchmarks.phase1 import BACKENDS, environment_metadata, make_loop, percentile


ROOT = Path(__file__).resolve().parents[1]
PROTOCOLS = ("protocol", "buffered")
NETWORKS = ("tcp", "unix")
WORKER_TIMEOUT_SECONDS = 180
WORKLOAD_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class TransportWorkload:
    protocol: str
    network: str
    connections: int
    message_size: int
    rounds: int
    slow_peer: bool = False
    peer_delay_us: int = 1000
    transport_mode: str = "python"

    def __post_init__(self):
        if self.protocol not in PROTOCOLS or self.network not in NETWORKS:
            raise ValueError("unknown protocol or network")
        if self.transport_mode not in {"python", "native"}:
            raise ValueError("unknown transport implementation")
        if min(self.connections, self.message_size, self.rounds) < 1 or self.peer_delay_us < 0:
            raise ValueError("workload dimensions must be positive; peer delay cannot be negative")
        if self.slow_peer and self.peer_delay_us == 0:
            raise ValueError("slow-peer workloads require a positive peer delay")

    @property
    def operations(self):
        return self.connections * self.rounds


def payload_for(size: int, sequence: int) -> bytes:
    body = (bytes(range(251)) * (size // 251 + 1))[:size]
    # Different rounds expose duplicated/reordered frames as well as corruption.
    marker = sequence.to_bytes(8, "little")[: min(size, 8)]
    return marker + body[len(marker) :]


def class_name(instance) -> str:
    cls = type(instance)
    return f"{cls.__module__}.{cls.__qualname__}"


class _FramedEndpoint:
    def __init__(self, workload, payloads, *, echo, failures):
        self.workload = workload
        self.payloads = payloads
        self.echo = echo
        self.failures = failures
        self.loop = asyncio.get_running_loop()
        self.closed = self.loop.create_future()
        self.writable = asyncio.Event()
        self.writable.set()
        self.transport = None
        self.transport_class = None
        self.received = bytearray()
        self.sequence = 0
        self.response = None
        self.resume_handle = None
        self.pause_count = 0
        self.resume_count = 0
        self.failure = None

    def connection_made(self, transport):
        self.transport = transport
        self.transport_class = class_name(transport)
        try:
            if self.workload.slow_peer:
                sock = transport.get_extra_info("socket")
                if sock is not None:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16 * 1024)
                transport.set_write_buffer_limits(high=32 * 1024, low=8 * 1024)
                if self.echo:
                    self._delay_reading()
        except Exception as error:
            self._fail(error)

    def _delay_reading(self):
        if self.resume_handle is not None:
            self.resume_handle.cancel()
        self.transport.pause_reading()
        self.resume_handle = self.loop.call_later(self.workload.peer_delay_us / 1_000_000, self._resume_reading)

    def _resume_reading(self):
        self.resume_handle = None
        if not self.transport.is_closing():
            self.transport.resume_reading()

    def pause_writing(self):
        self.pause_count += 1
        self.writable.clear()

    def resume_writing(self):
        self.resume_count += 1
        self.writable.set()

    def connection_lost(self, exc):
        if self.resume_handle is not None:
            self.resume_handle.cancel()
            self.resume_handle = None
        self.failure = exc or EOFError("transport closed")
        self.writable.set()
        if self.response is not None and not self.response.done():
            self.response.set_exception(self.failure)
        if not self.closed.done():
            self.closed.set_result(None)

    def eof_received(self):
        if self.received:
            self._fail(EOFError("transport closed in the middle of a frame"))
        return False

    def _fail(self, error):
        self.failures.append(error)
        self.failure = error
        if self.response is not None and not self.response.done():
            self.response.set_exception(error)
        self.transport.abort()

    def _accept_data(self, data):
        self.received.extend(data)
        size = self.workload.message_size
        while len(self.received) >= size:
            frame = bytes(self.received[:size])
            del self.received[:size]
            if self.sequence >= len(self.payloads) or frame != self.payloads[self.sequence]:
                self._fail(RuntimeError("transport benchmark payload mismatch or unexpected frame"))
                return
            self.sequence += 1
            if self.echo:
                self.transport.write(frame)
                if self.workload.slow_peer and self.sequence < self.workload.rounds:
                    self._delay_reading()
            elif self.response is None or self.response.done():
                self._fail(RuntimeError("transport benchmark received an unsolicited response"))
                return
            else:
                self.response.set_result(None)

    async def request(self, payload):
        if self.failure is not None:
            raise self.failure
        self.response = self.loop.create_future()
        started = time.perf_counter_ns()
        self.transport.write(payload)
        # Include producer backpressure in the round-trip measurement.
        await self.writable.wait()
        await self.response
        self.response = None
        return time.perf_counter_ns() - started


class FramedProtocol(_FramedEndpoint, asyncio.Protocol):
    def data_received(self, data):
        self._accept_data(data)


class FramedBufferedProtocol(_FramedEndpoint, asyncio.BufferedProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.receive_buffer = bytearray(64 * 1024)

    def get_buffer(self, sizehint):
        return self.receive_buffer

    def buffer_updated(self, nbytes):
        self._accept_data(memoryview(self.receive_buffer)[:nbytes])


async def transport_workload(workload: TransportWorkload) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    payloads = [payload_for(workload.message_size, sequence) for sequence in range(workload.rounds)]
    protocol_cls = FramedProtocol if workload.protocol == "protocol" else FramedBufferedProtocol
    endpoints = []
    failures = []
    clients = []
    server = None

    def factory(echo):
        endpoint = protocol_cls(workload, payloads, echo=echo, failures=failures)
        endpoints.append(endpoint)
        return endpoint

    async def client(address):
        if workload.network == "tcp":
            _, endpoint = await loop.create_connection(lambda: factory(False), *address[:2])
        else:
            _, endpoint = await loop.create_unix_connection(lambda: factory(False), address)
        latencies = [await endpoint.request(payload) for payload in payloads]
        if endpoint.sequence != workload.rounds or endpoint.received:
            raise RuntimeError("transport benchmark did not verify all response bytes")
        return latencies

    with tempfile.TemporaryDirectory(prefix="uringloop-phase3-") as directory:
        try:
            if workload.network == "tcp":
                server = await loop.create_server(lambda: factory(True), "127.0.0.1", 0)
                address = server.sockets[0].getsockname()
            else:
                address = str(Path(directory) / "echo.sock")
                server = await loop.create_unix_server(lambda: factory(True), address)
            clients = [asyncio.create_task(client(address)) for _ in range(workload.connections)]
            samples = await asyncio.gather(*clients)
            if failures:
                raise failures[0]
            if any(endpoint.sequence != workload.rounds or endpoint.received for endpoint in endpoints):
                raise RuntimeError("transport benchmark did not verify all request bytes")
            result = {
                "latencies": [latency for connection in samples for latency in connection],
                "transport_classes": sorted({endpoint.transport_class for endpoint in endpoints}),
                "pause_writing_calls": sum(endpoint.pause_count for endpoint in endpoints),
                "resume_writing_calls": sum(endpoint.resume_count for endpoint in endpoints),
                "verified_bytes": workload.operations * workload.message_size * 2,
            }
        except Exception:
            if failures:
                raise failures[0]
            raise
        finally:
            for task in clients:
                if not task.done():
                    task.cancel()
            if clients:
                await asyncio.gather(*clients, return_exceptions=True)
            for endpoint in endpoints:
                if endpoint.transport is not None:
                    endpoint.transport.abort()
            closed = [endpoint.closed for endpoint in endpoints if endpoint.transport is not None]
            if closed:
                await asyncio.wait_for(asyncio.gather(*closed), timeout=5)
            if server is not None:
                server.close()
                await server.wait_closed()
    return result


def run_workload(loop, workload):
    return loop.run_until_complete(asyncio.wait_for(transport_workload(workload), WORKLOAD_TIMEOUT_SECONDS))


def error_result(backend, workload, error):
    denied = isinstance(error, OSError) and error.errno in {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EOPNOTSUPP}
    unavailable = isinstance(error, ImportError) or isinstance(error.__cause__, ImportError)
    return {
        "status": "unsupported" if denied or unavailable else "error",
        "backend": backend,
        "workload": asdict(workload),
        "samples": [],
        "summary": None,
        "error": {"type": type(error).__name__, "message": str(error), "errno": getattr(error, "errno", None)},
    }


def worker_result(backend, workload, warmup, repeats):
    if backend not in BACKENDS or warmup < 0 or repeats < 1:
        raise ValueError("unknown backend or invalid sample counts")
    loop = None
    result = None
    try:
        if workload.transport_mode == "native":
            if backend not in {"native", "native-phase2"}:
                raise ValueError("native transports require a native ring backend")
            from uringloop.loop import IoUringProactorEventLoop
            from uringloop.proactor import IoUringProactor

            loop = IoUringProactorEventLoop(
                IoUringProactor(),
                loop_core="phase2" if backend == "native-phase2" else "phase1",
                transports="native",
            )
        else:
            loop = make_loop(backend)
        asyncio.set_event_loop(loop)
        for _ in range(warmup):
            run_workload(loop, workload)
        samples = []
        latencies = []
        transports = set()
        for _ in range(repeats):
            cpu_started = time.process_time_ns()
            wall_started = time.perf_counter_ns()
            outcome = run_workload(loop, workload)
            wall_ns = time.perf_counter_ns() - wall_started
            cpu_ns = time.process_time_ns() - cpu_started
            if len(outcome["latencies"]) != workload.operations or not outcome["transport_classes"]:
                raise RuntimeError("worker did not produce a complete verified transport sample")
            if outcome["verified_bytes"] != workload.operations * workload.message_size * 2:
                raise RuntimeError("worker byte count does not match the verified workload")
            transports.update(outcome["transport_classes"])
            latencies.extend(outcome["latencies"])
            samples.append(
                {
                    "wall_seconds": wall_ns / 1_000_000_000,
                    "cpu_seconds": cpu_ns / 1_000_000_000,
                    "operations_per_second": workload.operations * 1_000_000_000 / wall_ns,
                    "verified_bytes": outcome["verified_bytes"],
                    "pause_writing_calls": outcome["pause_writing_calls"],
                    "resume_writing_calls": outcome["resume_writing_calls"],
                }
            )
        walls = [sample["wall_seconds"] for sample in samples]
        runtime = {
            "loop_class": class_name(loop),
            "proactor_class": class_name(loop._proactor) if hasattr(loop, "_proactor") else None,
            "selector_class": class_name(loop._selector) if hasattr(loop, "_selector") else None,
            "loop_core": getattr(loop, "_loop_core_mode", "external"),
            "transport_classes": sorted(transports),
            "requested_transports": workload.transport_mode,
            "uringloop_transport_implementation": (
                "native-one-shot"
                if "uringloop.native_stream.NativeStreamTransport" in transports
                else "python-counted-send"
                if "uringloop.stream_transport.IoUringSocketTransport" in transports
                else None
            ),
            "buffered_protocol_strategy": "application-staging-buffer" if workload.protocol == "buffered" else None,
        }
        result = {
            "status": "ok",
            "backend": backend,
            "workload": asdict(workload),
            "warmup_runs": warmup,
            "measured_runs": repeats,
            "runtime": runtime,
            "samples": samples,
            "summary": {
                "wall_seconds_median": statistics.median(walls),
                "wall_time_cv_percent": statistics.stdev(walls) / statistics.mean(walls) * 100 if len(walls) > 1 else 0,
                "cpu_seconds_median": statistics.median(sample["cpu_seconds"] for sample in samples),
                "operations_per_second_median": statistics.median(sample["operations_per_second"] for sample in samples),
                "latency_p50_us": percentile(latencies, 0.50),
                "latency_p99_us": percentile(latencies, 0.99),
                "latency_samples": len(latencies),
                "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            },
        }
    except Exception as error:
        result = error_result(backend, workload, error)
    finally:
        cleanup_errors = []
        if loop is not None:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as error:
                cleanup_errors.append(error)
            try:
                loop.close()
            except Exception as error:
                cleanup_errors.append(error)
        try:
            asyncio.set_event_loop(None)
        except Exception as error:
            cleanup_errors.append(error)
        if cleanup_errors and result is not None:
            # Do not hide the original denied operation behind cleanup failure,
            # and do not report successful timings if cleanup itself failed.
            if result["status"] == "ok":
                result = error_result(backend, workload, cleanup_errors[0])
                result["status"] = "error"
            result["cleanup_errors"] = [
                {"type": type(error).__name__, "message": str(error), "errno": getattr(error, "errno", None)}
                for error in cleanup_errors
            ]
    return result


def matrix(profile, transport_mode="python"):
    if profile not in {"smoke", "full"}:
        raise ValueError("unknown benchmark profile")
    workloads = []
    for protocol in PROTOCOLS:
        for network in NETWORKS:
            for slow_peer in (False, True):
                dimensions = [(1, 262144 if slow_peer else 64, 2)]
                if profile == "full":
                    dimensions = [
                        (connections, size, 100 if connections == 1 else 20 if connections == 64 else 5)
                        for connections in (1, 64, 1000)
                        for size in (64, 4096, 262144)
                    ]
                workloads.extend(
                    TransportWorkload(protocol, network, *dimension, slow_peer, transport_mode=transport_mode)
                    for dimension in dimensions
                )
    return workloads


def worker_command(backend, workload, warmup, repeats):
    command = [
        sys.executable,
        "-m",
        "benchmarks.phase3",
        "--worker",
        "--backend",
        backend,
        "--protocol",
        workload.protocol,
        "--network",
        workload.network,
        "--transports",
        workload.transport_mode,
        "--connections",
        str(workload.connections),
        "--message-size",
        str(workload.message_size),
        "--rounds",
        str(workload.rounds),
        "--peer-delay-us",
        str(workload.peer_delay_us),
        "--warmup",
        str(warmup),
        "--repeats",
        str(repeats),
    ]
    if workload.slow_peer:
        command.append("--slow-peer")
    return command


def validate_result(result, backend, workload, repeats):
    if not isinstance(result, dict) or result.get("backend") != backend or result.get("workload") != asdict(workload):
        raise ValueError("isolated worker returned mismatched workload metadata")
    if result.get("status") not in {"ok", "unsupported", "error"}:
        raise ValueError("isolated worker returned an unknown status")
    if result["status"] == "ok":
        samples = result.get("samples")
        summary = result.get("summary")
        runtime = result.get("runtime")
        if not isinstance(samples, list) or len(samples) != repeats or result.get("measured_runs") != repeats:
            raise ValueError("isolated worker success lacks measured samples")
        if not isinstance(summary, dict) or summary.get("latency_samples") != workload.operations * repeats:
            raise ValueError("isolated worker success lacks verified latency samples")
        if not isinstance(runtime, dict) or not runtime.get("transport_classes") or not runtime.get("loop_class"):
            raise ValueError("isolated worker success lacks actual transport classes")
        for sample in samples:
            if not isinstance(sample, dict) or sample.get("verified_bytes") != workload.operations * workload.message_size * 2:
                raise ValueError("isolated worker sample lacks verified bytes")
            for metric in ("wall_seconds", "operations_per_second"):
                value = sample.get(metric)
                if not isinstance(value, (int, float)) or not 0 < value < float("inf"):
                    raise ValueError("isolated worker sample lacks finite positive timings")
    elif result.get("samples") != [] or result.get("summary") is not None or not result.get("error"):
        raise ValueError("isolated worker failure must not contain success samples")
    return result


def run_isolated(backend, workload, warmup, repeats):
    try:
        process = subprocess.run(
            worker_command(backend, workload, warmup, repeats),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=WORKER_TIMEOUT_SECONDS,
        )
        try:
            result = validate_result(json.loads(process.stdout), backend, workload, repeats)
        except (ValueError, TypeError) as error:
            raise RuntimeError(f"invalid worker output (exit {process.returncode}): {process.stderr[-2000:]}") from error
        if process.returncode and result["status"] == "ok":
            raise RuntimeError(f"worker exited {process.returncode} after reporting success")
        return result
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
        return error_result(backend, workload, error)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--backend", choices=BACKENDS, default="native")
    parser.add_argument("--backends", nargs="+", choices=BACKENDS, default=["native", "cffi", "asyncio", "uvloop"])
    parser.add_argument("--protocol", choices=PROTOCOLS, default="protocol")
    parser.add_argument("--network", choices=NETWORKS, default="tcp")
    parser.add_argument("--transports", choices=("python", "native"), default="python")
    parser.add_argument("--connections", type=int, default=1)
    parser.add_argument("--message-size", type=int, default=64)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--slow-peer", action="store_true")
    parser.add_argument("--peer-delay-us", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    parser.add_argument("--output", type=Path)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.warmup < 0 or args.repeats < 1:
        parser.error("warmup cannot be negative; repeats must be positive")
    try:
        workload = TransportWorkload(
            args.protocol,
            args.network,
            args.connections,
            args.message_size,
            args.rounds,
            args.slow_peer,
            args.peer_delay_us,
            args.transports,
        )
    except ValueError as error:
        parser.error(str(error))
    environment = environment_metadata()
    environment["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    environment["git_dirty"] = bool(
        subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()
    )
    if args.worker:
        document = worker_result(args.backend, workload, args.warmup, args.repeats)
        document["environment"] = environment
    else:
        document = {
            "schema_version": 1,
            "report": "phase3-transport-scaffold",
            "measurement_scope": "connection-setup-verified-echo-and-cleanup",
            "profile": args.profile,
            "environment": environment,
            "results": [
                run_isolated(backend, item, args.warmup, args.repeats)
                for item in matrix(args.profile, args.transports)
                for backend in args.backends
            ],
        }
    serialized = json.dumps(document, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n")
    else:
        print(serialized)
    if args.worker:
        return 0 if document["status"] == "ok" else 2
    return 0 if all(result["status"] == "ok" for result in document["results"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
