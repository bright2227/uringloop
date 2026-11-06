# Phase 3 add_reader and add_writer

Branch: `feature/phase3-readiness`

Depends on: `feature/phase3-multishot-accept`.

## Deliverable and files

Add reactor-compatible reader/writer registrations using ring poll requests.
The current loop inherits proactor behavior and lacks this public facility;
the existing one-shot `prepare_poll` is the fallback building block. Touch
loop public methods, a private per-fd registration manager, native poll
preparation/completion flags, the private stub and unit/e2e contract tests.
Implement one-shot semantics before enabling multishot polling.

## Sequence and rules

1. Validate fd conversion, closed loop and callable arguments against the
   supported asyncio contracts. Capture registration context/debug metadata.
   Keep independent reader and writer registrations per descriptor.
2. Each installation carries a generation, callback Handle and poll request.
   Replacement cancels the old registration logically before publishing the
   new one. Cancellation and late readiness of an old generation cannot run
   the replacement callback or remove its request.
3. Removal returns the compatible boolean and disables queued callback
   delivery immediately. Repeated removal is harmless. Keep the native
   request owner alive until final CQE after cancellation.
4. Schedule bounded callbacks through the loop's selected ready mechanism.
   Level-triggered readiness can repeat, but it cannot recursively drain,
   flood unbounded Handles or starve timers. One-shot rearm happens after
   callback/state validation; multishot MORE-cleared events rearm similarly.
5. Poll ERR/HUP reaches the registered readiness callback using compatible
   semantics rather than synthesizing a transport EOF. Reject registrations
   on descriptors currently owned by loop transports, with a precise error
   compatible with the locked asyncio behavior.
6. Close cancels both directions and drops callback references while retaining
   kernel-safe request owners. Document caller responsibility to remove a
   registration before closing/reusing its raw fd; do not promise that the
   loop can detect every external close/reuse.

## Required tests and measurement

Socketpairs, pipes and eventfd; reader+writer simultaneous; replace/remove
before/after queued CQE; self-removal/replacement; callback closes or stops
loop; stale cancellation; fd reuse; unsupported multishot flag; persistent
writable readiness with a timer; debug/context behavior; invalid/closed fds;
transport-owned fd rejection. Fake completions prove generations and MORE
handling; real polling and ASAN/UBSAN prove fd/request shutdown safety.

Measure ready callbacks/sec, submissions, CPU and timer-delay distribution
for idle, burst and persistent readiness. Representative library smoke tests
can be added with pinned dependencies; library compatibility is not claimed
from API tests alone.

Exit requires exact replacement/removal semantics, no callback after logical
removal, bounded fairness, fallback tests and lightweight review free of
high/medium lifetime/context findings.
