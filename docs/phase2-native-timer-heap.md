# Phase 2 native timer heap

Branch: `feature/phase2-native-timer-heap`

Depends on: `feature/phase2-native-ready-queue`

## Purpose

Move timer ordering and due-timer promotion into native storage while keeping
the public Python timer-handle behavior unchanged.

## Planned work

- Store monotonic deadlines with a stable sequence number so equal-deadline
  timers have deterministic behavior matching the locked contracts.
- Retain strong, GC-visible references to Python timer handles during this
  milestone; native public handle types are deferred.
- Promote due timers into the native ready queue using the loop clock's
  resolution boundary and a bounded operation.
- Preserve lazy cancellation and compact the heap at a measured threshold so
  cancellation storms cannot create unbounded memory or scan costs.
- Expose the next live deadline without leaking cancelled entries into timeout
  calculation.
- Keep clock access injectable for deterministic tests and avoid wall-clock
  time entirely.

## Native boundary and API sketch

Extend `LoopCore` rather than `Ring`:

- `schedule_timer(handle, when) -> None`: insert a strong reference and its
  monotonic deadline using a failure-atomic heap operation.
- `timer_count` and `cancelled_timer_count`: expose diagnostics needed for
  tests and cleanup policy.
- `next_deadline() -> float | None`: discard cancelled roots as needed and
  return the next live deadline.
- `promote_due(now, resolution, limit) -> int`: move bounded due entries to the
  ready queue using the locked Phase 2 boundary semantics.
- `clear_timers() -> None`: release all references during loop close.

Whether equal deadlines require an explicit sequence number is decided by the
contracts branch; do not silently promise ordering stronger than supported
asyncio behavior.

## Expected code changes

- Native heap storage, growth, sift-up/down, compaction, traversal, and clear
  logic in the `_uringcore` source.
- Stub/ABI updates for the private `LoopCore` timer methods.
- `IoUringProactorEventLoop.call_at`, timeout calculation, cancelled-timer
  cleanup, and due-timer promotion routed through the experimental core.
- Fake-clock unit tests and real-loop integration tests for the Python and
  native heap adapters.

## Delivery sequence

1. Freeze deadline, equality, NaN/infinity, and clock-resolution behavior in
   tests.
1. Implement and fuzz the isolated heap against a Python `heapq` reference
   model over randomized schedule/cancel/promote operations.
1. Add compaction with the CPython policy as the correctness baseline, then
   tune only if measurement justifies a different documented threshold.
1. Wire the heap into the opt-in loop and run mixed ready/timer contracts.
1. Measure insertion, cancellation, next-deadline lookup, expiry, and peak
   memory across the published timer sizes.

## Failure and fairness rules

- Scheduling failure leaves the existing heap unchanged and does not retain
  the new handle.
- Promotion is bounded; remaining already-due timers force a nonblocking next
  tick rather than monopolizing the loop.
- Cancelled entries release references during root pruning or compaction and
  cannot affect the selected wait timeout.
- Loop close clears ready and timer storage even when callbacks form cycles.

## Required tests

- Equal, past, zero, very long, and near-resolution deadlines.
- Cancellation at the heap root and below it, including cancellation storms.
- Timer callbacks that schedule/cancel other timers during the same tick.
- Heap ownership, GC cycles, loop close, allocation failures, and monotonic
  clock jumps supplied by the test clock.
- Timer insertion and expiry benchmarks compared with the Phase 2 baseline.

## Non-goals

- No public native `TimerHandle`, timing wheel, calendar queue, wall-clock
  timers, or speculative alternate heap until the binary heap is measured.

## Exit criteria

Timer behavior matches the contract suite in debug and non-debug modes, the
heap stays bounded under cancellation stress, and sanitizer runs are clean.

The lightweight review checks heap invariants, cancellation accounting,
clock-boundary compatibility, ownership, and ABI/stub synchronization.

## Implemented design

- `_uringcore.LoopCore` now owns a failure-atomic binary timer heap alongside
  the ready queue. Entries retain GC-visible strong references and use a
  monotonic sequence number only to make the private heap deterministic; the
  public contract still does not promise equal-deadline FIFO ordering.
- Cancelled roots are pruned before timeout selection. Cancellation storms use
  an ownership-safe compaction pass at the same count/fraction threshold as
  CPython, and bounded promotion moves at most 4,096 due handles into the
  native ready queue per tick.
- `IoUringProactorEventLoop(timer_heap="native", ready_queue="native")` is the
  experimental opt-in. Both defaults remain Python-backed, and native timer
  storage requires the native ready adapter so promotion is one ownership
  transfer inside the shared core.
- ABI version 10 exposes timer scheduling, next-deadline lookup, bounded due
  promotion, compaction, diagnostics, and clear operations in the private
  stub. Direct randomized-model tests and fake-clock `_run_once` tests cover
  the native boundary without requiring a functioning kernel ring.

## Measurement interpretation

`benchmarks.timer_heap` compares native and `heapq` insertion, lazy
cancellation, next-deadline lookup, expiry, CPU/wall time, variation, and peak
RSS in isolated processes. It deliberately avoids ring setup and I/O so the
storage boundary is the only variable. Smoke data is retained for provenance
and regression detection; it is not a production performance claim.

## Captured smoke baseline

`benchmarks/results/phase2-timer-heap-2026-08-10-wsl2.json` was captured from
harness commit `4d6a368` on Python 3.12.3 / WSL2 with one warmup and five
measured runs of 4,096 scheduled timers, cancelling every second timer before
expiry. Median full-lifecycle throughput was 1.97 million timers/second for the
native core and 0.87 million timers/second for the Python reference. Wall-time
variation was 10.4% and 20.8%, respectively, so the raw samples are useful as
a smoke baseline but do not justify changing the default path.
