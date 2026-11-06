# Phase 3 size-gated SEND_ZC

Branch: `feature/phase3-send-zc`

Depends on: `feature/phase3-buffered-protocol`.

## Deliverable and files

Use existing `Ring.prepare_send_zc()` for explicitly selected large transport
writes. Native transport write ownership/queueing, request state routing,
the private stub, state-machine tests and benchmark tooling are the primary
touch points. Keep ordinary send available and compare it on identical data.

## Sequence and invariants

1. Add a validated threshold and enable control. Runtime opcode probe plus
   first-operation result chooses support. Do not infer support from kernel
   version or assume zero-copy actually occurred merely because SEND_ZC ran.
2. Retain an immutable owned write payload or a compatible safe snapshot of
   mutable input. Preserve ordered partial sends; advance logical bytes on
   operation completion, not on a later notification.
3. Operation completion and memory-release completion are separate events.
   If the operation indicates an outstanding notification, hold every
   kernel-referenced buffer until that notification or safe ring teardown.
   If no notification is expected according to actual CQE flags, finalize
   without waiting forever. Reuse the native request state machine.
4. Cancellation CQE does not authorize premature buffer release. Abort may
   notify connection-lost logically while a native owner survives pending
   notifications. Bound outstanding pinned bytes/request count; when the cap
   is hit, preserve byte order using queued/fallback sends and backpressure.
5. On unsupported error before bytes are transmitted, retry ordinary send
   once. A short successful operation is not replayed from byte zero. Ordinary
   errors preserve standard transport close/exception behavior.

## Required tests

Threshold boundaries, zero-length/small writes, partial operation results,
MORE/notification flags, delayed notification, operation error without
notification, cancel-before/after-success, notification after logical close,
mutable input, exporter GC and high-water transitions. Use scripted CQEs for
rare orderings and real-kernel large writes for notification lifetime.

ASAN/UBSAN stresses abort and loop close while notifications remain. Validate
the sum of outstanding pin accounting returns to zero; expose it separately
from unsent write bytes so get-write-buffer-size keeps asyncio semantics.

## Measurement and exit

Sweep payload sizes and concurrency, collecting CPU, throughput, latency,
RSS, pinned bytes, notification counts and ordinary-send fallback counts.
Record copy/zero-copy information only when exposed reliably by the actual
kernel interface. Select a threshold from controlled evidence, and record a
no-go if the complexity has no justified win.

Exit requires no high/medium ownership/arithmetic/fallback findings, a passing
notification-race suite and an explicit enablement decision. Threshold tuning
and default promotion are separate from demonstrating correctness.
