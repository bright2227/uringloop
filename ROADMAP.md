# Roadmap: a native-core uringloop competitive with uvloop

Status: draft for discussion — phases beyond the current baseline are not
implemented yet.

Last updated: 2026-07-28

## Goal

Evolve uringloop from a pure-Python + CFFI proactor into an event loop whose
hot paths run in native code on top of io_uring, with correctness,
compatibility, and performance comparable to uvloop — and better than uvloop
on workloads where io_uring has structural advantages, such as many small
operations, file I/O, and zero-copy sends.

"Competitive with uvloop" is defined by three bars, in priority order:

1. **Correctness**: pass an adapted CPython asyncio test suite. This requires
   a maintained harness rather than simply running `test.test_asyncio`: much
   of that suite instantiates loop classes directly or touches internals. The
   harness needs loop-factory injection, a reviewed skip list, a pinned
   CPython test version per supported Python version, and PSF-2.0 attribution
   for any copied test code.
1. **Compatibility**: work as a drop-in loop via
   `asyncio.run(main(), loop_factory=...)`, including reactor-style APIs such
   as `add_reader` and `add_writer` that are missing from proactor loops and
   are required by libraries including psycopg, pyzmq, and prompt_toolkit.
1. **Performance**: publish reproducible benchmark curves against stock
   asyncio and uvloop. Performance claims must name the workload, kernel,
   Python version, CPU, concurrency, message size, and statistic.

## Current baseline

As of 2026-07-28, `main` already includes the first correctness and
maintainability pass:

- request-owned CFFI resources and buffer-lifetime fixes;
- fd-leak, partial-send, EINTR, cancellation, and `_stop_serving` fixes;
- Python 3.14 child-watcher removal and pidfd-based subprocess handling;
- batched SQE submission;
- package metadata and CI matrix cleanup; and
- a consistent Linux 5.19 minimum in both code and documentation.

These changes are the Phase 0 starting point, not pending roadmap work. Their
behavior must be captured by regression tests before the native port begins.

## Why uvloop is fast, and what that implies

uvloop is Cython plus libuv, not hand-written C. Its speed comes primarily
from three properties:

1. **The loop core is native**: the ready queue, timer heap, and handles avoid
   much of the allocation and dispatch overhead of Python implementations.
1. **Transports and protocol dispatch are native**: bytes arrive in a native
   buffer and `protocol.data_received` is invoked without an intermediate
   stack of Python loop and transport frames. This is likely the largest
   single opportunity for uringloop as well.
1. **Correctness is a first-class deliverable**, backed by a mature test
   suite that includes behavior adapted from CPython.

The implementation order is therefore based on what moves into native code:
ring and ownership first, loop plumbing second, and transports third. Each
phase must deliver value on its own.

io_uring also has structural advantages over an epoll-backed design:

- multishot accept and recv can produce many completions from one SQE;
- provided-buffer rings let the kernel select from a bounded reusable pool;
- `IORING_OP_SEND_ZC` can reduce copying for sufficiently large writes; and
- file I/O can be asynchronous without routing ordinary operations through a
  Python thread pool.

These are opportunities, not automatic wins. Every feature needs a fallback
and a benchmark showing that it helps its intended workload.

## Guiding principles

- **Benchmark-driven**: no optimization merges without a number from the
  Phase 0 harness.
- **Correctness before speed**: the CPython-derived suite and uringloop's e2e
  tests are the primary progress indicators.
- **Keep the pure-Python implementation as the oracle**: run the same
  behavior tests against both implementations. Differential testing helps
  separate an event-loop semantic bug from a native memory or ownership bug.
- **Runtime feature probing, not version gating**: `io_uring_probe` and setup
  results select multishot, zero-copy, and task-run features. The kernel
  version remains only a documented support floor.
- **No silent degradation**: when io_uring is unavailable, the factory either
  raises a precise error or performs an explicitly requested selector-loop
  fallback with a visible warning.
- **Small reviewable milestones**: each phase is decomposed into issues with
  its own correctness test and measurement. A phase is not one large porting
  pull request.

## Phase 0 — foundations

**Goal**: establish instruments and behavioral contracts before replacing
the implementation.

- Build a benchmark harness covering:
  - echo throughput and latency at 1, 64, and 1,000 connections;
  - multiple message sizes and write-backpressure cases;
  - streams and transport/protocol APIs;
  - file I/O, with an explicitly named stock-asyncio baseline such as
    `asyncio.to_thread`; and
  - CPU time, RSS, syscall counts, throughput, and p50/p99 latency.
- Separate benchmark jobs from ordinary CI. Shared GitHub-hosted runners are
  useful for smoke and regression detection but too noisy for release claims.
  Publish authoritative curves only from a pinned machine or a controlled
  runner, with warmup, repeated samples, and recorded environment metadata.
- Build the CPython asyncio test harness described under the correctness bar.
  Track the upstream test revision, the reason for every skip, and the
  baseline pass rate for each supported Python version.
- Add feature-probe unit tests by injecting probe results so fallback branches
  do not depend solely on the host kernel. Run real-kernel integration jobs
  for 5.19, 6.1, and a current kernel on a scheduled or release workflow;
  QEMU does not need to block every small pull request.
- Convert the behavior fixed in PRs #4–#11 into focused regression tests,
  especially request lifetime, cancellation/completion races, partial sends,
  fd cleanup, and batched submission.

**Exit criteria**: reproducible performance results and a per-Python-version
test baseline are produced automatically. Benchmark noise is quantified
before any percentage target is used as a gate.

## Phase 1 — native ring and request core

**Goal**: replace CFFI ring management while preserving the Python proactor
API.

- Add a native extension, initially named `_uringcore`, that owns SQ/CQ
  management. It must not require a system-installed liburing at runtime.
  Spike both viable implementation routes:

  - raw `io_uring_setup`/`io_uring_enter` syscalls plus mmap; or
  - a statically linked, vendored liburing.

  The decision record must compare wheel size, license/attribution, build
  complexity, sanitizer coverage, syscall overhead, and the unsafe surface.
  Raw syscalls require correctly implementing the acquire/release ordering on
  shared ring heads and tails that liburing already handles.
- Move request lifetimes into native structs. A request owns every
  kernel-referenced buffer, sockaddr, msghdr, and iovec.
- Use a refcounted, multi-completion request state machine rather than
  "release on the first CQE." At minimum it must model submitted,
  cancel-pending, operation-complete, notification-pending, and released
  states. `SEND_ZC` normally produces an operation CQE followed by a
  notification CQE, and asynchronous cancellation can race with a successful
  operation completion.
- Add a bounded provided-buffer pool with explicit memory accounting and
  backpressure. Be precise about copying:

  - the pool removes per-receive kernel-buffer allocation;
  - materializing `PyBytes` still allocates and copies; and
  - a memoryview-based path avoids that copy only if its ownership and return
    to the pool are explicit and safe.

- Size the CQ explicitly for multishot workloads, detect overflow, drain
  promptly, and test re-arming when the kernel terminates a multishot
  operation by clearing `IORING_CQE_F_MORE`.
- Expose a small, GIL-conscious batch API: prepare operations, submit once per
  loop tick, and reap a batch of completions in one native call.
- Add EINTR handling, runtime opcode/feature probing, and registered-ring-fd
  support where probing and measurement justify it.

**Exit criteria**:

- existing unit and e2e behavior stays green;
- Linux wheels build and import without system liburing;
- ASAN and UBSAN e2e runs are clean;
- cancellation races, CQ pressure, multiple CQEs per request, buffer
  exhaustion, and multishot re-arm paths are covered; and
- the benchmark report shows the measured effect on CPU, syscalls, and
  latency. A speedup is not assumed merely because CFFI was removed.

**Go/no-go checkpoint**: do not replace the loop core until the native request
state machine is demonstrably correct. If Phase 1 does not improve a relevant
metric, retain it only if the packaging or safety benefit justifies its
maintenance cost.

## Phase 2 — native loop core

**Goal**: replace the hottest `BaseProactorEventLoop` plumbing without
prematurely porting rare APIs.

- Move the ready callback queue, timer heap, and hot handle operations into
  native code. Keep behavior identical in debug and non-debug modes.
- Implement `call_soon_threadsafe` wakeups with an eventfd polled by the ring.
  `IORING_OP_MSG_RING` is only an optional optimization when the sending
  thread already owns a suitable source ring; it is not a general replacement
  for cross-thread wakeups.
- Keep CPython's signal machinery and point `signal.set_wakeup_fd` at an
  eventfd polled by io_uring. Do not use signalfd as the default: blocking
  signals changes CPython signal semantics and can leak a blocked mask into
  subprocesses.
- Move the measured `run_forever` hot path into native code:
  submit/wait, drain CQEs, and run a bounded batch of ready callbacks.
- Implement native `Handle` and `TimerHandle` storage only after their exact
  compatibility surface is tested. Use freelists only where allocation
  profiles show value.
- Leave rare APIs such as `getaddrinfo` in Python until profiling justifies
  moving them.

**Exit criteria**: the CPython-derived suite is at or above the Phase 0
baseline, debug behavior remains correct, and the named scheduling benchmarks
beat the stock loop beyond the measured noise floor.

## Phase 3 — native transports and protocol dispatch

**Goal**: remove Python transport/proactor frames from the network hot path.

- Implement TCP and Unix stream transports in native code:
  - multishot recv backed by a bounded provided-buffer ring;
  - direct protocol method dispatch using vectorcall and cached method
    lookups;
  - write buffering with correct high/low watermarks; and
  - size-gated `IORING_OP_SEND_ZC` with buffer lifetime held until the
    notification CQE.
- Implement multishot accept so connection setup does not require a new
  accept SQE for every connection.
- Implement `add_reader` and `add_writer` with poll operations, including
  replacement/removal semantics, cancellation races, and multishot
  `IORING_CQE_F_MORE` handling.
- Treat `BufferedProtocol` as a separate receive path. Its contract calls
  `get_buffer()` and writes into that returned buffer, while multishot recv
  requires kernel-selected provided buffers. Start with one-shot recv directly
  into `get_buffer()` followed by `buffer_updated()`. Do not claim a
  zero-copy combination with multishot provided buffers unless a safe design
  and benchmark demonstrate it.
- Add UDP and pipe transports, then retain the existing splice-based sendfile
  path.
- Decide the re-entrancy model before implementation. A protocol callback
  invoked during CQE draining may call `write`, `close`, or `loop.stop`.
  Either defer submissions/state transitions until the drain completes or
  make all affected paths explicitly re-entrant; document and test the chosen
  rule.
- Reuse CPython's `sslproto` initially. A native SSL transport is a separate
  project justified only by profiling.
- Keep subprocess handling on the pidfd design.

**Exit criteria**: plaintext echo benchmarks reach parity with or exceed
uvloop for the published workload matrix, while flow control, cancellation,
EOF, half-close, and re-entrancy tests remain green.

## Phase 4 — correctness and compatibility completion

- Bring the CPython-derived suite to the agreed pass target, with only
  documented platform or deliberate-design skips.
- Complete debug mode, slow-callback logging, exception-handler paths, signal
  behavior, reactor APIs, and representative third-party compatibility tests.
- Detect an unavailable or seccomp-blocked io_uring setup at startup. Expose a
  clear exception and an opt-in fallback mode; never silently run stock
  asyncio when the caller believes uringloop is active.
- Define the free-threaded Python stance:
  - one `IORING_SETUP_SINGLE_ISSUER` ring per loop;
  - documented loop/thread affinity;
  - synchronized cross-thread scheduling; and
  - an explicit `Py_mod_gil` declaration backed by tests on a free-threaded
    build.

## Phase 5 — performance program

- Evaluate io_uring features independently:
  `IORING_SETUP_DEFER_TASKRUN`, `IORING_SETUP_COOP_TASKRUN`,
  `IORING_SETUP_SINGLE_ISSUER`, registered files for hot sockets, registered
  buffers, and zero-copy sends.
- Evaluate native-side techniques independently: handle/request freelists,
  vectorcall, cached protocol methods, and avoiding dict-based kwargs in hot
  paths.
- Choose zero-copy thresholds empirically. `SEND_ZC` can still copy and always
  adds lifetime/completion complexity; it is not expected to win for every
  write size.
- Gate every optimization on the Phase 0 methodology and publish regressions
  as well as wins.

### Kernel feature matrix

Runtime probes and setup results are authoritative; versions below indicate
the upstream introduction point, not permission to assume a feature works in
the current container or environment.

| Feature | Introduced |
|---|---:|
| multishot poll | Linux 5.13 |
| registered ring fd | Linux 5.18 |
| `MSG_RING` | Linux 5.18 |
| multishot accept | Linux 5.19 |
| provided-buffer ring | Linux 5.19 |
| `IORING_SETUP_COOP_TASKRUN` | Linux 5.19 |
| multishot recv | Linux 6.0 |
| `SEND_ZC` | Linux 6.0 |
| `IORING_SETUP_SINGLE_ISSUER` | Linux 6.0 |
| `SENDMSG_ZC` | Linux 6.1 |
| `IORING_SETUP_DEFER_TASKRUN` | Linux 6.1 |

The project keeps Linux 5.19 as the support floor. Features introduced later
must have probe-gated fallback paths.

## Phase 6 — productization

- Build manylinux and musllinux wheels with cibuildwheel and no system
  liburing dependency.
- Default to per-Python-version wheels. Revisit abi3 only after the public
  native API and free-threaded stance stabilize; Stable ABI availability of
  vectorcall alone is not enough reason to commit to abi3.
- Publish compatibility, kernel/container requirements, benchmark
  methodology, benchmark results, and troubleshooting documentation.
- Adopt semantic versioning and retain the pure-Python implementation as a
  behavioral oracle until the native path is demonstrably mature.

## Risks and honest notes

- **Scale**: uvloop represents many years of native event-loop engineering
  and edge-case fixes. At side-project pace, the complete roadmap is likely a
  multi-year effort. Each phase therefore needs standalone user value.
- **Native C versus Cython**: the ring, buffer, and request core suit C.
  Loop and transport classes may be safer and much smaller in Cython. Choose
  based on maintenance and profiling, not on a "pure C" identity; the goal is
  a native hot path.
- **CPython internals**: inheriting from or copying private asyncio behavior
  creates a compatibility burden across Python releases. Pin test sources and
  treat every supported Python minor version as a separate contract.
- **Benchmark noise**: shared CI hardware can easily manufacture apparent
  wins or regressions. Store raw samples and environment metadata, not only a
  single summary number.
- **Deployment reality**: Docker Engine 25+ and containerd's default seccomp
  profiles block io_uring syscalls, and some Google environments disable
  io_uring. Deployment detection and an explicit fallback story are required.
- **Multi-completion ownership**: multishot operations, zero-copy
  notifications, and asynchronous cancellation invalidate the current
  one-user-data/one-CQE cache model. This is the principal correctness risk
  and the Phase 1 critical path.
- **Ordering discipline**: benchmarks and ownership tests must precede
  large-scale native rewrites. Otherwise it will be impossible to distinguish
  faster code from changed semantics or newly introduced lifetime bugs.

## Non-goals

- Windows or macOS support; io_uring is Linux-specific.
- A new async API unrelated to asyncio; the target is drop-in asyncio
  compatibility.
- Rewriting low-traffic Python APIs merely to maximize the amount of native
  code.
