# Roadmap: a dependable io_uring event loop for asyncio

Status: foundations, a small native I/O core, compatibility, and usable
releases take priority. Native scheduling and native transports are the last
two phases and are optional high-performance experiments.

Last updated: 2026-11-07

## Phase order

| Phase | Focus | Role |
|---|---|---|
| 0 | Foundations: tests and reproducible benchmarks | Core |
| 1 | Native ring and request core; Python loop and transports | Core |
| 2 | Correctness and asyncio compatibility | Core |
| 3 | Performance measurement and targeted tuning | Measure first; optimizations need evidence |
| 4 | Packaging, documentation, and releases | Core |
| 5 | Native loop core and scheduling | Optional high performance; formerly Phase 2 |
| 6 | Native transports and protocol dispatch | Optional high performance; formerly Phase 3 |

The baseline product can reach Phase 4 while retaining Python scheduling and
transports. Phase 3 establishes measurements and can conclude that no tuning
is justified; a speedup or uvloop parity is not a baseline release gate.
Phases 5 and 6 require separate evidence and explicit opt-in.

Historical phase numbers, branch names, API values, and evidence filenames
are explained in the [phase numbering map](docs/phase-numbering.md).

## Goal

Deliver a correct, installable Linux asyncio event loop backed by io_uring.
The baseline keeps event-loop scheduling, proactor orchestration, and
transports in Python, with a small C extension owning the ring and request
resources. This follows the division used by CPython's Windows proactor:
native I/O operations with Python asyncio plumbing.

The baseline is judged by three bars, in priority order:

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

Competing with uvloop through native scheduling and transports is a separate
optional objective in Phases 5 and 6. It must justify its maintenance cost
with controlled measurements rather than expanding the amount of C code by
default.

## Current baseline

As of 2026-11-07, remote `main` is at `adb0a7e`, which includes the revert of
PR #23 through PR #25. The Python/CFFI proactor remains active, alongside the
early `_uringcore` ring lifecycle and NOP submission/completion foundation.
The reverted native request/proactor integration and native scheduling stack
must not be counted as completed work on `main`.

PR #24 remains open as experimental work with historical phase numbering.
Its existence and passing branch checks do not make the baseline phases
complete or authorize restoring all reverted prerequisites together.
Baseline correctness fixes can be reviewed separately from optional native
scheduling or transport implementations.

The immediate work is to establish the Phase 0 gates and finish Phase 1 in
small steps, then complete baseline compatibility and release readiness.
Phase numbers describe priorities, not a claim that every earlier exit gate
has already passed.

## Why uvloop is fast, and what that implies

uvloop is Cython plus libuv, not hand-written C. Its speed comes primarily
from three properties:

1. **The loop core is native**: the ready queue, timer heap, and handles avoid
   much of the allocation and dispatch overhead of Python implementations.
1. **Transports and protocol dispatch are native**: bytes arrive in a native
   buffer and `protocol.data_received` is invoked without an intermediate
   stack of Python loop and transport frames. This is a possible optimization
   for uringloop that still needs profiling and measurement.
1. **Correctness is a first-class deliverable**, backed by a mature test
   suite that includes behavior adapted from CPython.

These properties motivate optional experiments, not the baseline
implementation order. Ring ownership comes first; compatibility and usable
releases follow with the Python loop and transports. Only measured bottlenecks
justify the later native loop and transport phases.

io_uring also has structural advantages over an epoll-backed design:

- multishot accept and recv can produce many completions from one SQE;
- provided-buffer rings let the kernel select from a bounded reusable pool;
- `IORING_OP_SEND_ZC` can reduce copying for sufficiently large writes; and
- file I/O can be asynchronous without routing ordinary operations through a
  Python thread pool.

These are opportunities, not automatic wins. Every feature needs a fallback
and a benchmark showing that it helps its intended workload.

## Guiding principles

- **Keep the baseline small**: Python scheduling and transports remain the
  reference architecture. Optional Phases 5 and 6 are separate decisions, not
  prerequisites for completing compatibility or shipping a release.
- **Safety applies to every retained path**: correctness and lifetime fixes
  remain required even when the code under test is an optional experiment.
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

Keep callback scheduling, timers, and transport logic in Python. Complete
one-shot socket/file operations, polling, cancellation, and owned request
lifetimes before considering advanced kernel features. The native loop and
transport rewrites belong to optional Phases 5 and 6.

- Add a native extension, initially named `_uringcore`, that owns SQ/CQ
  management. It must not require a system-installed liburing at runtime.
  Spike both viable implementation routes:

  - raw `io_uring_setup`/`io_uring_enter` syscalls plus mmap; or
  - a statically linked, vendored liburing.

  The decision record must compare wheel size, license/attribution, build
  complexity, sanitizer coverage, syscall overhead, and the unsafe surface.
  Raw syscalls require correctly implementing the acquire/release ordering on
  shared ring heads and tails that liburing already handles. The
  [Phase 1 backend decision](docs/phase1-native-ring-decision.md), accepted on
  2026-08-01, selects the statically linked, vendored liburing route and
  records the measurements and required follow-up.

- Move request lifetimes into native structs. A request owns every
  kernel-referenced buffer, sockaddr, msghdr, and iovec.

- If multishot operations or `SEND_ZC` are enabled in a measured experiment,
  use a refcounted, multi-completion request state machine rather than
  "release on the first CQE." At minimum it must model submitted,
  cancel-pending, operation-complete, notification-pending, and released
  states. `SEND_ZC` normally produces an operation CQE followed by a
  notification CQE, and asynchronous cancellation can race with a successful
  operation completion.

- Provided-buffer pools are optional kernel-feature work for Phases 3 or 6,
  not a prerequisite for the one-shot baseline. When added, require bounded
  memory accounting and backpressure. Be precise about copying:

  - the pool removes per-receive kernel-buffer allocation;
  - materializing `PyBytes` still allocates and copies; and
  - a memoryview-based path avoids that copy only if its ownership and return
    to the pool are explicit and safe.

- Size the CQ explicitly, detect overflow, and drain promptly. If multishot is
  enabled, test re-arming when the kernel terminates an operation by clearing
  `IORING_CQE_F_MORE`.

- Expose a small, GIL-conscious batch API: prepare operations, submit once per
  loop tick, and reap a batch of completions in one native call.

- Add EINTR handling, runtime opcode/feature probing, and registered-ring-fd
  support where probing and measurement justify it.

**Exit criteria**:

- existing unit and e2e behavior stays green;
- Linux wheels build and import without system liburing;
- ASAN and UBSAN e2e runs are clean;
- cancellation races, CQ pressure, failed registration/initialization, and
  reentrant resource cleanup are covered; advanced multi-CQE, buffer-pool,
  and multishot paths have their own gates before being enabled; and
- the benchmark report shows the measured effect on CPU, syscalls, and
  latency. A speedup is not assumed merely because CFFI was removed.

**Go/no-go checkpoint**: require a demonstrably correct native request core
before integration. A measured packaging or safety benefit can justify the
small native backend even without a speedup. Passing Phase 1 does not approve
optional Phase 5 scheduling or Phase 6 transports.

## Phase 2 — correctness and compatibility completion

**Goal**: make the baseline asyncio implementation dependable while keeping
scheduling and transports in Python.

- Bring the CPython-derived suite to the agreed pass target, with only
  documented platform or deliberate-design skips.
- Complete debug mode, slow-callback logging, exception-handler paths, signal
  behavior, reactor APIs, and representative third-party compatibility tests.
- Implement `add_reader` and `add_writer` using the ring's poll operations and
  Python callback dispatch, including replacement/removal and cancellation
  races. Native dispatch or multishot poll is a later optional optimization.
- Cover partial and zero-progress writes, flow control, EOF, half-close,
  cancellation, SSL/BufferedProtocol, pipes, UDP, and subprocess behavior in
  the baseline. These correctness fixes do not depend on a native transport.
- Detect an unavailable or seccomp-blocked io_uring setup at startup. Expose a
  clear exception and an opt-in fallback mode; never silently run stock
  asyncio when the caller believes uringloop is active.
- Define the free-threaded Python stance:
  - one ring per loop, with `SINGLE_ISSUER` used only when probed and justified;
  - documented loop/thread affinity;
  - synchronized cross-thread scheduling; and
  - an explicit `Py_mod_gil` declaration backed by tests on a free-threaded
    build.

**Exit criteria**: the agreed compatibility target and supported-library
checks pass for the baseline on each supported Python version, with clear
platform limitations and no unresolved ownership or cancellation failures.

## Phase 3 — performance measurement and targeted tuning

**Goal**: measure the baseline and evaluate small optimizations independently.
A reproducible baseline and a documented no-go decision are useful outcomes;
this phase does not require a native scheduler or transport rewrite.

- Evaluate io_uring features independently:
  `IORING_SETUP_DEFER_TASKRUN`, `IORING_SETUP_COOP_TASKRUN`,
  `IORING_SETUP_SINGLE_ISSUER`, registered files for hot sockets, registered
  buffers, and zero-copy sends.
- Keep any accepted tuning compatible with the Python scheduling/transport
  baseline. Native scheduling, handle storage, and protocol dispatch belong
  to optional Phases 5 and 6 and need their own bottleneck evidence.
- Choose zero-copy thresholds empirically. `SEND_ZC` can still copy and always
  adds lifetime/completion complexity; it is not expected to win for every
  write size.
- Gate every optimization on the Phase 0 methodology and publish regressions
  as well as wins.

**Exit criteria**: publish an auditable baseline, noise estimates, and the
reason for accepting or deferring each experiment. A release can retain the
untuned baseline if proposed optimizations do not justify their complexity.

## Phase 4 — productization

**Goal**: ship an installable, documented baseline without requiring either
optional high-performance phase.

- Build manylinux and musllinux wheels with cibuildwheel and no system
  liburing dependency.
- Default to per-Python-version wheels. Revisit abi3 only after the public
  native API and free-threaded stance stabilize; Stable ABI availability of
  vectorcall alone is not enough reason to commit to abi3.
- Publish compatibility, kernel/container requirements, benchmark
  methodology, benchmark results, and troubleshooting documentation.
- Adopt semantic versioning and retain the pure-Python implementation as a
  behavioral oracle until the native path is demonstrably mature.

**Exit criteria**: source and wheel installation, supported-platform checks,
documentation, and release validation pass. Every shipped path must meet its
safety and compatibility gates; uvloop parity and enabling Phases 5 or 6 are
not baseline release requirements.

## Phase 5 — optional native loop core

**Goal**: optionally replace measured scheduling bottlenecks in
`BaseProactorEventLoop`, after the baseline is correct and usable.

**Entry gate**: Phase 0 measurements identify a scheduling bottleneck worth
the maintenance cost, and the baseline compatibility and release gates pass.
Legacy Phase 2 implementations and benchmarks are reference experiments,
not permission to make native scheduling the default.

- Move the ready callback queue, timer heap, and hot handle operations into
  native code. Keep behavior identical in debug and non-debug modes.
- Implement `call_soon_threadsafe` wakeups with an eventfd polled by the ring.
  `IORING_OP_MSG_RING` is only an optional optimization when the sending
  thread already owns a suitable source ring; it is not a general replacement
  for cross-thread wakeups.
- Keep CPython signals on a byte-oriented socket/pipe wakeup fd. Eventfd is
  only for thread scheduling: `signal.set_wakeup_fd` writes one byte, while
  eventfd accepts eight-byte counter writes. Do not use signalfd as the
  default, because blocking signals changes Python/subprocess semantics.
- Move the measured `run_forever` hot path into native code:
  submit/wait, drain CQEs, and run a bounded batch of ready callbacks.
- Implement native `Handle` and `TimerHandle` storage only after their exact
  compatibility surface is tested. Use freelists only where allocation
  profiles show value.
- Leave rare APIs such as `getaddrinfo` in Python until profiling justifies
  moving them.

**Exit criteria**: compatibility and debug behavior remain at or above the
baseline, and controlled scheduling benchmarks beat the retained Python
path beyond measured noise without material end-to-end regressions. Keep
the implementation opt-in until these gates justify promotion; otherwise
defer it and retain Python scheduling.

## Phase 6 — optional native transports and protocol dispatch

**Goal**: optionally remove Python transport/proactor frames where controlled
workloads demonstrate enough benefit to justify native lifecycle complexity.

**Entry gate**: the baseline compatibility/release gates pass and profiling
identifies transport overhead as a bottleneck. Evaluate transport changes
against Python scheduling first; enabling Phase 5 is not a runtime
prerequisite. Legacy Phase 3 work, including PR #24, remains experimental.

- Implement TCP and Unix stream transports in native code:
  - multishot recv backed by a bounded provided-buffer ring;
  - direct protocol method dispatch using vectorcall and cached method
    lookups;
  - write buffering with correct high/low watermarks; and
  - size-gated `IORING_OP_SEND_ZC` with buffer lifetime held until the
    notification CQE.
- Implement multishot accept so connection setup does not require a new
  accept SQE for every connection.
- Optionally optimize the reader/writer APIs completed in Phase 2 with native
  dispatch or multishot poll, preserving replacement/removal and cancellation
  semantics and handling `IORING_CQE_F_MORE` correctly.
- Treat `BufferedProtocol` as a separate receive path. Its contract calls
  `get_buffer()` and writes into that returned buffer, while multishot recv
  requires kernel-selected provided buffers. Start with one-shot recv directly
  into `get_buffer()` followed by `buffer_updated()`. Do not claim a
  zero-copy combination with multishot provided buffers unless a safe design
  and benchmark demonstrate it.
- Add native UDP and pipe transports only if justified, with the Python
  transports retained as the baseline; preserve the existing sendfile path.
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

This is an optional performance-program target, not a release gate for the
baseline product. Keep native transports opt-in until controlled results
justify promotion, and publish regressions and no-go decisions as well as wins.

## Kernel feature matrix

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
  one-user-data/one-CQE cache model. Correct cancellation ownership is a
  Phase 1 requirement; advanced multi-completion paths need their own safety
  gates before they are enabled in later experiments.
- **Ordering discipline**: benchmarks and ownership tests must precede
  large-scale native rewrites. Otherwise it will be impossible to distinguish
  faster code from changed semantics or newly introduced lifetime bugs.

## Non-goals

- Windows or macOS support; io_uring is Linux-specific.
- Requiring native scheduling or native transports to ship the baseline.
- A new async API unrelated to asyncio; the target is drop-in asyncio
  compatibility.
- Rewriting low-traffic Python APIs merely to maximize the amount of native
  code.
