# Phase 1 benchmark report

## Decision

Proceed to Phase 2. In the measured smoke workloads, the native proactor
improves median throughput and process CPU time over the retained CFFI oracle
without increasing `io_uring_enter` calls. The packaging and ownership-safety
benefits independently justify retaining the native core. These measurements
do not establish release-level percentage gates because the WSL2 smoke-run
noise is high.

## Method

`benchmarks/phase1.py` runs every backend in a fresh process. The backends are
the public native proactor, the private CFFI behavioral oracle, and stock
asyncio. Workloads cover low-level socket echo, streams (therefore the
transport/protocol path), and file reads; stock file reads use
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
  --syscalls --output phase1.json
```

Environment: CPython 3.12.3, x86-64 WSL2, Linux
6.18.33.2-microsoft-standard-WSL2, 8 logical CPUs. Measurements were taken on
2026-08-08 at integration commit `e9e6027`.

## Results

| Workload | Backend | ops/s median | CPU seconds median | p50 µs | p99 µs | wall CV | relevant wait syscalls |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| socket, 1×64 B×50 | native | 12,873 | 0.003793 | 54.2 | 205.2 | 29.4% | 104 `io_uring_enter` |
| | CFFI | 9,083 | 0.005378 | 80.1 | 278.2 | 26.4% | 104 `io_uring_enter` |
| | asyncio | 11,318 | 0.004318 | 68.1 | 278.4 | 14.1% | 207 `epoll_wait` |
| streams, 1×4 KiB×25 | native | 5,833 | 0.004186 | 97.0 | 446.9 | 32.5% | 110 `io_uring_enter` |
| | CFFI | 4,592 | 0.005314 | 141.2 | 863.8 | 26.2% | 110 `io_uring_enter` |
| | asyncio | 5,587 | 0.004370 | 126.3 | 938.6 | 33.9% | 117 `epoll_wait` |
| file, 50×4 KiB | native | 38,466 | 0.001272 | 13.4 | 104.0 | 55.5% | 53 `io_uring_enter` |
| | CFFI | 27,189 | 0.001806 | 21.4 | 95.6 | 23.6% | 53 `io_uring_enter` |
| | asyncio `to_thread` | 4,407 | 0.009964 | 174.4 | 597.7 | 17.2% | 104 `epoll_wait` |

The native/CFFI syscall counts are identical for each workload: the Phase 1
gain comes from less Python/CFFI boundary work, not a claimed syscall
reduction. Relative to CFFI, median native throughput is about 42% higher for
socket echo, 27% higher for streams, and 41% higher for file reads; median CPU
time falls by roughly 29%, 21%, and 30%, respectively. The high coefficients
of variation, especially for the short file sample, prohibit treating those
percentages as stable performance guarantees.

## Correctness baseline

`scripts/compat_baseline.py` runs the pinned CPython-derived asyncio behavior
suite and emits JUnit-derived JSON with pass rate and every skip reason. The
local Python 3.12 baseline is 7/7 passing, with no failures, errors, or skips.
CI records separate artifacts for Python 3.12 (`v3.12.3` behavior revision)
and 3.13 (`v3.13.0`). The native and CFFI proactors also run the same uringloop
proactor e2e suite as differential correctness coverage.

## Remaining measurement work

Before making release claims, run the full profile on a pinned bare-metal or
otherwise controlled host, repeat enough times to bring CV below the selected
gate, and publish the raw JSON artifact. Phase 2 scheduling benchmarks must
use that controlled baseline and exceed its measured noise floor.
