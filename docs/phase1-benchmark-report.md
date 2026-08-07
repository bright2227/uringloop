# Phase 1 benchmark report

## Decision

Proceed to Phase 2. In the measured smoke workloads, the native proactor
improves median throughput and process CPU time over the retained CFFI oracle
without increasing `io_uring_enter` calls. The packaging and ownership-safety
benefits independently justify retaining the native core. These measurements
do not establish release-level percentage gates because the WSL2 smoke-run
noise is high. uvloop is faster than the native loop in both networking smoke
workloads, giving Phase 2's scheduling and loop-core work a meaningful external
target.

## Method

`benchmarks/phase1.py` runs every backend/configuration in a fresh process. The
backends are the public native proactor, the private CFFI behavioral oracle,
stock asyncio, and pinned uvloop 0.21.0. Each worker creates one event loop,
warms it, reuses that loop and its ring for all measured repeats, and closes it
afterward. Loop/ring construction is therefore excluded from the samples;
workload resources such as sockets, servers, and temporary files are recreated
on each repeat.

Workloads cover low-level socket echo, streams (therefore the
transport/protocol path), and file reads; asyncio and uvloop file reads use
`asyncio.to_thread`. The full profile expands echo tests across 1, 64, and
1,000 connections and 64, 4,096, and 262,144-byte messages. Large socket
messages use a deliberately small send buffer to exercise write backpressure.

Each result records warmup count, individual repeated samples, wall and CPU
time, throughput, p50/p99 operation latency, peak RSS, wall-time coefficient
of variation, and environment metadata. Optional `strace -c` collection runs
outside the measured samples and records syscall counts. The weekly/manual
benchmark workflow is separate from ordinary CI; GitHub-hosted results are
smoke/regression evidence, not authoritative release curves.

The command used for this report was:

```console
python benchmarks/phase1.py --profile smoke --warmup 2 --repeats 10 \
  --syscalls --output benchmarks/results/phase1-2026-08-08-wsl2.json
```

Environment: CPython 3.12.3, x86-64 WSL2, Linux
6.18.33.2-microsoft-standard-WSL2, 8 logical CPUs. Measurements were taken on
2026-08-08 from the committed harness at `712ad43`. The complete raw result,
including every sample and the unabridged `strace -c` summaries, is retained in
[`benchmarks/results/phase1-2026-08-08-wsl2.json`](../benchmarks/results/phase1-2026-08-08-wsl2.json).

## Results

| Workload | Backend | ops/s median | CPU seconds median | p50 µs | p99 µs | wall CV | relevant wait syscalls |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| socket, 1×64 B×50 | native | 14,325 | 0.003419 | 52.5 | 221.8 | 23.8% | 104 `io_uring_enter` |
| | CFFI | 10,286 | 0.004747 | 84.5 | 287.1 | 25.6% | 104 `io_uring_enter` |
| | asyncio | 13,168 | 0.003714 | 62.9 | 293.9 | 20.4% | 207 `epoll_wait` |
| | uvloop | 17,373 | 0.002808 | 46.3 | 153.1 | 9.1% | 110 `epoll_pwait`, 201 `io_uring_enter` |
| streams, 1×4 KiB×25 | native | 6,595 | 0.003701 | 95.1 | 446.8 | 44.5% | 110 `io_uring_enter` |
| | CFFI | 4,978 | 0.004903 | 143.3 | 386.2 | 10.4% | 110 `io_uring_enter` |
| | asyncio | 5,332 | 0.004581 | 144.4 | 478.6 | 18.8% | 117 `epoll_wait` |
| | uvloop | 11,851 | 0.002052 | 36.1 | 423.6 | 47.1% | 66 `epoll_pwait`, 7 `io_uring_enter` |
| file, 50×4 KiB | native | 51,152 | 0.000956 | 12.9 | 52.8 | 17.3% | 53 `io_uring_enter` |
| | CFFI | 25,667 | 0.001906 | 22.9 | 159.1 | 28.7% | 53 `io_uring_enter` |
| | asyncio `to_thread` | 5,199 | 0.008476 | 177.3 | 667.7 | 20.0% | 104 `epoll_wait` |
| | uvloop `to_thread` | 4,878 | 0.008959 | 165.9 | 796.0 | 23.4% | 107 `epoll_pwait`, 2 `io_uring_enter` |

The native/CFFI syscall counts are identical for each workload: the Phase 1
gain comes from less Python/CFFI boundary work, not a claimed syscall
reduction. Relative to CFFI, median native throughput is about 39% higher for
socket echo, 32% higher for streams, and 99% higher for file reads; median CPU
time falls by roughly 28%, 25%, and 50%, respectively. The high coefficients
of variation in several very short samples prohibit treating those percentages
as stable performance guarantees.

## Correctness baseline

`scripts/compat_baseline.py` runs seven CPython-derived asyncio contract tests
and emits JUnit-derived JSON with pass rate and every skip reason. Every local
test maps to an upstream method in `tests/compat/upstream.json`; CI downloads
the exact source files and verifies their SHA-256 digests before publishing a
baseline. The local Python 3.12 baseline is 7/7 passing, with no failures,
errors, or skips. CI records separate artifacts for CPython 3.12 at
`f6650f9ad73359051f3e558c2431a109bc016664` (`v3.12.3`) and 3.13 at
`60403a5409ff2c3f3b07dd2ca91a7a3e096839c7` (`v3.13.0`). This is an auditable
compatibility subset, not the full CPython asyncio suite. The native and CFFI
proactors also run the same uringloop proactor e2e suite as differential
correctness coverage.

## Remaining measurement work

Before making release claims, run the full profile on a pinned bare-metal or
otherwise controlled host and repeat enough times to bring CV below the
selected gate. Phase 2 scheduling benchmarks must use that controlled baseline
and exceed its measured noise floor. Correctness work should also expand the
seven-test, hash-verified CPython compatibility subset.
