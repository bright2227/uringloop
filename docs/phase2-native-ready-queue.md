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
