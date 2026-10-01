# Phase 2 eventfd wakeups

Branch: `feature/phase2-eventfd-wakeup`

Depends on: `feature/phase2-native-timer-heap`

## Purpose

Use a ring-polled eventfd to wake an idle loop for cross-thread scheduling,
while preserving CPython-compatible signal delivery and shutdown behavior.

## Planned work

- Create one nonblocking, close-on-exec eventfd per loop and keep one poll
  request armed through the native ring.
- Coalesce redundant thread wakeups, drain the eventfd counter completely, and
  re-arm after completions or recoverable cancellation races.
- Synchronize enqueue-before-wakeup ordering and cover loop close racing with
  a foreign thread calling `call_soon_threadsafe`.
- Retain a separate nonblocking pipe/socket wakeup fd for CPython signals.
  `signal.set_wakeup_fd` writes single bytes, whereas eventfd accepts only
  eight-byte counter writes, so an eventfd cannot directly satisfy that API.
- Keep signal handlers in CPython; do not block signals or make signalfd the
  default.
- Probe eventfd/poll setup failures and raise a precise initialization error
  rather than silently disabling thread wakeups.

## Descriptor and state design

- Create eventfd with `EFD_NONBLOCK | EFD_CLOEXEC` and store its ownership in
  the native loop core.
- Represent the armed poll request and closing state explicitly so completion,
  cancellation, and re-arm cannot create two simultaneous polls.
- Maintain a coalescing flag synchronized with ready-queue publication: the
  producer enqueues first, then only the transition from not-signalled to
  signalled writes the eight-byte eventfd value.
- On completion, drain until `EAGAIN`, clear the coalescing state with the
  required synchronization, recheck for newly published work, and re-arm.
- Keep the existing signal wakeup descriptor on a distinct code path and make
  ownership/close order explicit.

## Expected code changes

- Native eventfd creation, write, drain, poll-request state, cancellation, and
  teardown in `_uringcore` and its type stub.
- A focused native operation or helper for the ring-polled eventfd; do not
  expose the descriptor as a public user API.
- `IoUringProactorEventLoop.call_soon_threadsafe`, self-reading setup/cleanup,
  and close logic routed through the experimental wakeup implementation.
- Stress and signal tests in `tests/e2e/loop/`, with low-level state/failure
  tests in `tests/unit/` or `tests/e2e/native/`.

## Delivery sequence

1. Add a standalone eventfd capability/failure spike and confirm behavior on
   the supported Linux floor and under seccomp-like injected failures.
1. Implement descriptor ownership and direct write/drain tests.
1. Arm it through `Ring.prepare_poll`, including cancellation and re-arm state
   tests.
1. Wire single-producer `call_soon_threadsafe`, then add many-producer stress.
1. Separate and verify the CPython signal wakeup path before removing the old
   thread-wakeup socketpair.
1. Measure wakeup latency, writes per burst, poll completions, and CPU.

## Error and shutdown behavior

- Retry `EINTR`; treat `EAGAIN` as an already-pending wakeup, not loss.
- A write after loop close follows asyncio's closed-loop error behavior.
- Setup failure aborts experimental-loop construction with all descriptors and
  requests released.
- Closing cancels/reaps the poll request before closing its fd, tolerates a
  concurrent producer, and never lets an fd number be reused while referenced
  by an in-flight request.

## Required tests

- Idle-loop wakeup from one and many producer threads, including burst
  coalescing and wakeup-counter saturation handling.
- Queue visibility before wakeup, stop/close races, fd closure, fork/exec
  inheritance flags, and repeated loop construction.
- SIGINT and custom Python signal handlers while the ring wait is idle.
- Cross-thread wakeup latency and syscall-count measurements against the Phase
  1 socketpair path.

## Non-goals

- No `IORING_OP_MSG_RING` default, signalfd, process-shared eventfd, or GIL-free
  mutation of Python callback objects.

## Exit criteria

Thread-safe callbacks never wedge or disappear, signal semantics remain
compatible, all wakeup descriptors are released, and stress/sanitizer runs are
clean.

The lightweight review checks publish/wakeup ordering, poll lifetime, fd close
order, signal separation, error handling, and the measured syscall claim.

## Implemented design

- Private `_uringcore.EventFD` ownership uses `EFD_NONBLOCK | EFD_CLOEXEC`,
  coalesces notifications under the GIL, retries `EINTR`, treats counter
  saturation as an already-pending wakeup, drains to `EAGAIN`, and closes
  idempotently. ABI version 11 includes private write/read diagnostics and
  deterministic setup-error injection.
- `IoUringProactorEventLoop(thread_wakeup="eventfd")` arms one `POLLIN`
  request, drains and re-arms it after completion, and re-arms recoverable
  cancellation races. Close first prevents re-arm, then cancels the request,
  closes the proactor/ring, and only then closes the descriptor.
- Callback publication still occurs in `BaseEventLoop.call_soon_threadsafe`
  before `_write_to_self` notifies eventfd. The existing socketpair and
  self-reading request remain dedicated to CPython's byte-oriented signal
  wakeup contract; eventfd is never passed to `signal.set_wakeup_fd`.
- The default remains `thread_wakeup="socketpair"`. Direct descriptor tests,
  a fake-proactor state-machine suite, multi-producer e2e coverage, signal
  delivery, setup failure, saturation, and close behavior cover the opt-in.

## Measurement interpretation

`benchmarks.eventfd_wakeup` measures cross-thread idle latency, burst writes,
drain reads, CPU/wall time, variation, and RSS for eventfd and the socketpair
reference in isolated processes. The restricted development sandbox blocks
socket writes, so a committed local smoke capture may contain the eventfd side
only; CI runs both backends, while the prior Phase 2 scheduling baseline
remains the auditable socketpair loop reference. No syscall or latency
improvement is claimed from a one-sided local capture.

## Captured smoke baseline

`benchmarks/results/phase2-eventfd-wakeup-2026-08-10-wsl2.json` was captured
from harness commit `5995820` on Python 3.12.3 / WSL2 with one warmup and five
measured runs. Each run used 64 serialized idle notifications and a 4,096-call
burst from four producers. Eventfd performed the expected 64 idle writes and
one coalesced burst write; median idle latency was 30.2 microseconds at p50 and
67.3 microseconds at p99, with 14.4% wall-time variation. The socketpair side
could not execute under the local syscall sandbox and is intentionally absent,
so these figures validate instrumentation and coalescing only.
