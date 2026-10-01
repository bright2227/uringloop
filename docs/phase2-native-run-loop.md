# Phase 2 native run-loop tick

Branch: `feature/phase2-native-run-loop`

Depends on: `feature/phase2-eventfd-wakeup`

## Purpose

Move the measured loop-iteration plumbing into native code without turning the
entire public `run_forever` lifecycle into an opaque C control loop.

## Planned work

- Add one native tick operation that computes the next timeout, submits queued
  SQEs, waits when allowed, drains a bounded CQE batch, promotes due timers,
  and removes a bounded ready batch.
- Release the GIL only during the blocking ring wait. Reacquire it before
  touching futures, handles, exceptions, contexts, or other Python objects.
- Preserve `stop()` semantics: the current ready snapshot completes, while
  newly scheduled callbacks remain for the next iteration.
- Apply explicit fairness budgets for CQEs, timers, and ready callbacks and
  reschedule immediate work rather than blocking when a budget is exhausted.
- Route Python callback execution and exception handling through a small,
  reviewable adapter initially; moving callback invocation into C requires a
  separate measured change.
- Preserve debug timing, slow-callback reporting, async-generator hooks, and
  Python 3.12/3.13 lifecycle differences.

## Tick contract

The proposed internal operation returns control to Python once per bounded
tick. Inputs include the current monotonic time and CQ/ready budgets; outputs
include the ready snapshot and whether immediate work remains. It must follow
this order unless the contracts establish a different observable requirement:

1. Detect already-ready callbacks or due timers and force a zero wait.
1. Flush prepared ring work exactly once.
1. Wait only when no immediate work exists, releasing the GIL around that wait.
1. Drain at most the CQ budget and resolve completions under the GIL.
1. Promote at most the timer budget.
1. Snapshot at most the ready budget for Python dispatch.
1. Mark another immediate tick when any budget was exhausted.

## Expected code changes

- Extend `LoopCore` with a tick/result representation and coordination with the
  existing `Ring` object without merging their ownership domains.
- Add a private native-proactor batch interface so completion resolution does
  not bounce through one Python `select()` call per event.
- Override the narrow `_run_once`/lifecycle hooks needed by supported Python
  versions in `uringloop/loop.py`; keep version-specific adapters explicit.
- Add stub/ABI changes and fault-injection seams for submit, wait, CQ drain,
  timer promotion, and batch creation.
- Extend scheduling benchmarks with mixed ready/timer/CQE workloads and
  starvation counters.

## Delivery sequence

1. Instrument the Phase 1 loop to confirm where each scheduling workload
   spends CPU and choose initial budgets from data.
1. Implement an isolated nonblocking tick and differential state-model tests.
1. Add timeout calculation and the GIL-free blocking wait with concurrent
   wakeup/cancellation stress.
1. Add bounded CQ and timer processing, then bounded ready snapshots.
1. Wire the experimental loop path while retaining Python callback dispatch.
1. Tune budgets only using mixed-load fairness and latency results, not maximum
   single-workload throughput alone.

## Safety rules

- No borrowed `PyObject *` survives a GIL release; required objects hold owned
  references in GC-visible native state.
- A wait guard prevents submit/reap/close from another thread while the native
  ring wait is active.
- EINTR retries preserve the original absolute timeout and allow Python signal
  handling before sleeping again.
- Any partial tick failure leaves the loop closable and does not discard a CQE,
  timer, or ready handle.
- Callback execution remains outside the GIL-free section and uses the existing
  exception handler.

## Required tests

- Empty, ready-only, timer-only, I/O-only, and mixed iterations.
- Stop/restart, callback-recursion, callback exceptions, cancellation races,
  signal interruption, EINTR, and cross-thread wakeup during the GIL-free wait.
- Fairness stress with continuously ready callbacks and CQEs.
- Fault injection at submit, wait, reap, timer promotion, and callback handoff.
- Scheduling throughput, CPU, p50/p99 delay, RSS, and syscall measurements.

## Non-goals

- No unbounded C `run_forever` loop, direct protocol dispatch, native transport,
  DNS work, or callback execution without returning to the Python adapter.

## Exit criteria

The native tick matches the oracle behavior, cannot block with immediate work
pending, stays responsive under mixed load, and improves a named scheduling
metric beyond measured noise.

The lightweight review checks tick ordering, timeout arithmetic, GIL/reference
safety, fairness budgets, version adapters, and failure-state recoverability.

## Implemented design

- ABI version 12 adds `LoopCore.prepare_tick` for native timeout arithmetic
  and `finish_tick` for bounded due-timer promotion plus an owned ready-list
  handoff. Partial failures leave promoted work in the ready queue and never
  discard a timer or callback.
- `IoUringProactorEventLoop(run_loop="native")` is an experimental opt-in that
  requires the already-reviewed native ready queue and timer heap. It keeps
  callback execution, debug timing, exception handling, lifecycle hooks, and
  the outer one-tick return in Python.
- The native proactor's private `select_batch` waits through `Ring.wait`, which
  already releases the GIL only around the kernel wait, resolves at most 256
  completions, and reports residual CQ work for an immediate follow-up tick.
  CFFI remains a supported differential selector and uses its existing batch
  behavior.
- Initial budgets are 256 CQ completions, 4,096 due timers, and 4,096 ready
  callbacks. Exhaustion counters and an immediate-next-tick flag prevent any
  exhausted category from causing a blocking wait. Callback snapshots that
  survive a `BaseException` remain ahead of newly scheduled work.
- The default remains the Python `_run_once` path. Contract matrices cover the
  native tick with native and CFFI proactors while fake-selector tests exercise
  timeout, stop/snapshot, recursion, and budget behavior without kernel access.

## Measurement interpretation

`benchmarks.run_loop_tick` isolates mixed ready/timer tick preparation and
handoff against an equivalent Python state model, reporting throughput, CPU,
variation, RSS, tick count, and both storage-budget exhaustion counts. It does
so with the configured 4,096-item ready and timer budgets, which are embedded
in every result for auditability. It does not include callback bodies or claim
end-to-end loop speed; those remain in the Phase 2 scheduling matrix.

## Captured smoke baseline

`benchmarks/results/phase2-native-tick-2026-08-10-wsl2.json` was captured from
harness commit `1c7e74e` on Python 3.12.3 / WSL2 with one warmup and five
measured runs of 4,096 ready plus 4,096 due timer handles. Both models required
two ticks and recorded the same single ready-budget exhaustion and no timer-
budget exhaustion. Median state-plumbing throughput was 4.58 million
handles/second for the native core and 2.96 million for the Python model, with
48.7% and 14.9% wall variation. The short smoke run is therefore retained as
auditable functional evidence, not a performance conclusion; it does not show
an end-to-end event-loop or application improvement.
