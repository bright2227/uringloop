# Phase 2 loop contracts and baselines

Branch: `feature/phase2-loop-contracts`

## Purpose

Establish the behavioral and measurement contracts needed to change the loop
core safely. This branch changes tests and benchmarks, not event-loop runtime
behavior.

## Planned work

- Expand the hash-attributed CPython compatibility subset for:
  - `call_soon`, `call_later`, and `call_at` ordering;
  - cancellation before and during a loop tick;
  - `Handle` and `TimerHandle` equality, ordering, repr, and debug metadata;
  - callback `contextvars` capture and exception-handler routing;
  - loop stop/restart behavior and callbacks scheduling callbacks; and
  - thread-affinity and `call_soon_threadsafe` behavior.
- Add injectable clock tests for equal-deadline timers, clock resolution,
  cancelled-timer cleanup, and long-delay boundaries.
- Add scheduling benchmarks for ready-callback batches, timer insertion and
  expiry, mixed I/O/timers, and cross-thread wakeup latency.
- Run stock asyncio, uvloop, the native proactor loop, and the retained CFFI
  oracle in isolated workers with warmup and raw samples.
- Record Phase 1 values as the pre-native-loop baseline rather than claiming
  a performance target from noisy smoke data.

## Expected code and data changes

- `tests/compat/test_cpython_asyncio.py`: add adapted scheduling, timer,
  context, debug, and exception contracts.
- `tests/compat/upstream.json`: map every adaptation to the exact supported
  CPython 3.12 and 3.13 sources and hashes.
- `tests/e2e/loop/`: add real-loop stop/restart, thread, signal, and mixed-work
  scenarios that cannot be proved with isolated unit tests.
- `tests/unit/`: add deterministic fake-clock and handle-surface tests.
- `benchmarks/phase2.py`: add an isolated scheduling benchmark runner without
  changing the Phase 1 workload schema or historical result.
- `.github/workflows/benchmarks.yml`: publish Phase 2 smoke artifacts
  separately from the Phase 1 result.

## Delivery sequence

1. Inventory the relevant CPython test methods and record exact provenance
   before adapting assertions.
1. Add fake-clock and direct loop-contract tests; verify they pass on the
   unchanged Phase 1 loop.
1. Add real-loop and cross-thread scenarios with bounded timeouts so a wedged
   loop fails deterministically.
1. Add benchmark workers and validate each backend in a separate process.
1. Capture a raw Phase 1-loop scheduling baseline at the committed harness
   revision.

## Benchmark definitions

- Ready queue: enqueue and execute 1, 64, and 4,096 callbacks per tick.
- Timers: insert/cancel/expire 1, 1,000, and 100,000 timers. Distributed timers
  receive a 250 ms insertion lead and span two seconds, so their deadlines do
  not collapse into an already-overdue equal-deadline batch.
- Mixed tick: ready callbacks plus socket completions plus due timers. Ready
  queue delay, timer lateness, and socket round-trip latency are reported as
  separate p50, p99, and maximum service-delay distributions.
- Cross-thread idle wakeup: one producer submits one callback, waits for its
  acknowledgement, then allows the consumer to re-enter its kernel wait before
  the next measured submission. Wakeup latency is the primary metric.
- Cross-thread burst contention: one and four producers submit without the idle
  handshake; this separately measures enqueue contention and callback draining.
- Metrics: wall time, process CPU, operations/s, p50/p99 latency, RSS, syscall
  counts, CV, Python allocations where reliable, and environment metadata.

## Non-goals and risks

- Do not modify `uringloop/loop.py`, the extension ABI, or default loop
  selection on this branch.
- Do not copy CPython's helper framework wholesale; adaptations must stay small
  and attributed under the included PSF license.
- Very short scheduling samples are noise-sensitive. Increase work per sample
  before increasing claims, and never turn hosted-runner percentages into a
  release gate.

## Exit criteria

- The new fake-clock, handle, real-loop, and compatibility contracts pass on
  both the native and retained CFFI proactors without production loop changes.
- Every compatibility adaptation records its exact CPython method, commit,
  source hash, adaptation summary, and skip reason if applicable.
- Benchmark smoke jobs are deterministic enough for regression detection;
  authoritative gates remain reserved for a controlled runner.
- The lightweight branch review finds no missing contract required by the
  next ready-queue branch.

## Lightweight review checklist

- Production code is unchanged.
- Every new contract passes against the Phase 1 native and retained oracle
  paths where both apply.
- Provenance verification and JSON-only report output remain intact.
- Benchmark operation counts and backend isolation match the documented
  methodology.
