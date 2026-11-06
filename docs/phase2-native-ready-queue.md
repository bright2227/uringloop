# Phase 2 native ready queue

Branch: `feature/phase2-native-ready-queue`

Depends on: `feature/phase2-loop-contracts`

## Purpose

Move ready-queue storage and batch removal into the native extension while
continuing to execute ordinary Python `asyncio.Handle` callbacks in Python.
This isolates ownership and ordering risk before native timer or dispatch work.

## Planned work

- Add a GC-aware native loop-core object instead of overloading the Phase 1
  `Ring` object's request-lifetime responsibilities.
- Store strong references to ready handles in FIFO order and expose only the
  minimum enqueue, length, clear, and bounded-pop operations needed by the
  Python loop adapter.
- Preserve the CPython rule that callbacks scheduled by a running callback do
  not run until the next loop iteration.
- Skip cancelled handles at dispatch without changing observable ordering of
  the remaining callbacks.
- Make close/clear idempotent and ensure cyclic references through callbacks,
  contexts, or loop objects are visible to Python's cyclic GC.
- Keep a private Python deque implementation available as the differential
  oracle until Phase 2 integration.

## Native boundary and API sketch

Add a `LoopCore` heap type to `uringloop._uringcore`. It initially owns only
ready-queue state; ring ownership stays with `Ring`.

- `enqueue_ready(handle) -> None`: append exactly one strong reference.
- `ready_count -> int`: report queued entries, including cancelled handles not
  yet removed.
- `take_ready_batch(limit) -> list[Handle]`: snapshot at most the count present
  at tick start; callbacks appended during dispatch remain queued.
- `clear_ready() -> None`: release every reference safely and idempotently.

The first version may return a Python list because callback execution remains
in Python. Avoid a more complex iterator/borrowed-reference API until the
native tick branch can consume queue entries directly.

## Expected code changes

- `src/uringcore_liburing.c`: register `LoopCore`, including traverse, clear,
  dealloc, failure-atomic growth, and module initialization.
- `uringloop/_uringcore.pyi`: document the new private native surface and bump
  the internal ABI version.
- `uringloop/loop.py`: override the minimum scheduling/drain hooks necessary to
  use `LoopCore`; retain a private deque-backed adapter for differential tests.
- `tests/unit/` and `tests/e2e/loop/`: add native queue ownership, ordering, and
  real-loop dispatch coverage.
- `MANIFEST.in` and `setup.py`: change only if the C implementation is split
  into additional source/header files.

## Delivery sequence

1. Add the isolated native type and direct unit tests without wiring it into
   the event loop.
1. Prove GC, allocation-failure, resize, and clear behavior under sanitizers.
1. Add an opt-in loop adapter and run every ready-queue contract against both
   adapters.
1. Enable the native queue for the Phase 2 experimental loop path only.
1. Measure enqueue, batch removal, and end-to-end callback throughput before
   retaining any micro-optimization.

## Failure and concurrency rules

- Enqueue is failure-atomic: on allocation failure the previous queue remains
  valid and the new handle is not retained.
- The loop thread mutates the queue under the GIL. Cross-thread producers use
  `call_soon_threadsafe`; this branch does not introduce GIL-free queue access.
- Clearing or closing twice is harmless. Use-after-close raises a precise
  error instead of silently recreating storage.
- A callback exception cannot lose or duplicate the unprocessed snapshot.

## Required tests

- FIFO and next-tick ordering across empty, single-item, and large batches.
- Cancellation before dequeue and during callback execution.
- Callback/context ownership, cyclic GC, close with pending handles, and
  exception cleanup under fault injection.
- Bounded-batch fairness against continuously arriving I/O completions.
- ASAN/UBSAN stress for repeated enqueue, drain, clear, and loop destruction.

## Non-goals

- No timer storage, eventfd, GIL-free callback dispatch, public native handle,
  or default-path switch.
- No freelist or lock-free queue without a separate profile and memory-model
  design.

## Exit criteria

The native queue passes the loop contracts and differential tests, introduces
no leaks or sanitizer findings, and does not yet replace Python callback
execution or timer management.

The lightweight review must explicitly check reference ownership, snapshot
semantics, ABI/stub agreement, and the absence of unrelated Ring changes.

## Implemented design

- `_uringcore.LoopCore` is a separate GC-aware heap type with a failure-atomic
  growable FIFO, strong-reference traversal, deterministic allocation-failure
  injection through `max_ready`, idempotent clear/close, and precise
  use-after-close errors. The native ABI version is 9.
- `_NativeReadyQueue` adapts `LoopCore` to the private deque operations used by
  `BaseEventLoop`. Its first `popleft` transfers one bounded tick snapshot into
  Python; callbacks appended during dispatch remain in the native core for the
  next tick. An interrupted batch stays ahead of newly queued callbacks.
- `IoUringProactorEventLoop(ready_queue="native")` is the experimental opt-in.
  The default remains `ready_queue="python"`, preserving the private deque
  oracle until the integration branch.
- Compatibility, fake-clock, and real-loop scheduling contracts run across the
  native and CFFI proactors with both ready-queue adapters.

## Measurement interpretation

The branch-specific runner compares direct enqueue/batch removal and callback
dispatch through `BaseEventLoop._run_once` in isolated processes. The callback
case uses a minimal loop with no proactor so ring setup and completion waits do
not contaminate a measurement whose only variable is ready-queue storage.
Initial smoke measurements
show that native storage can improve direct queue throughput while the Python
adapter boundary can erase that improvement end to end. This branch therefore
retains the native path only as an experimental ownership boundary and makes no
performance claim. A raw result tied to the committed harness is required
before branch completion.

## Captured smoke baseline

`benchmarks/results/phase2-ready-queue-2026-08-10-wsl2.json` was captured from
harness commit `7173d9f` on Python 3.12.3 / WSL2 with one warmup and five
measured runs of 4,096 callbacks. The direct medians were 27.0 million native
and 20.4 million deque handle lifecycles per second. The `BaseEventLoop`
dispatch medians were 580 thousand native-adapter and 600 thousand deque
callbacks per second. The deque direct sample had 38.5% wall-time variation,
so these smoke figures are provenance data and regression input, not a stable
performance conclusion.
