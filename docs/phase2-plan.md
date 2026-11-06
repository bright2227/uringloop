# Phase 2 implementation plan

## Starting point

Phase 2 starts from `feature/phase1-benchmark-report` at `bc1f331`. Phase 1
already provides the native ring/request core, native proactor integration,
sanitizer coverage, a CFFI behavioral oracle, and an auditable benchmark
baseline. Phase 2 must preserve those contracts while moving only measured
event-loop hot paths into native code.

No Phase 2 branch is merged by this plan. The branches below are a linear,
documentation-only stack until implementation begins.

## Branch stack

| Order | Branch | Depends on | Planned outcome |
| ---: | --- | --- | --- |
| 1 | `feature/phase2-loop-contracts` | Phase 1 report | Lock scheduling, timer, handle, debug, and benchmark contracts before native changes. |
| 2 | `feature/phase2-native-ready-queue` | loop contracts | Add a native FIFO ready queue while retaining Python callback execution. |
| 3 | `feature/phase2-native-timer-heap` | ready queue | Add native monotonic timer ordering, cancellation, and promotion into the ready queue. |
| 4 | `feature/phase2-eventfd-wakeup` | timer heap | Replace socketpair wakeups with an eventfd polled by the ring, including signal-safe behavior. |
| 5 | `feature/phase2-native-run-loop` | eventfd wakeup | Move submit/wait, CQ draining, timer promotion, and bounded ready batching into the native loop core. |
| 6 | `feature/phase2-native-handles` | native run loop | Introduce compatible native `Handle` and `TimerHandle` storage only after their surface is measured and tested. |
| 7 | `feature/phase2-integration-report` | native handles | Make the Phase 2 path the integrated default, run differential/sanitizer/package gates, and publish results. |

Each branch receives the completed commit from the branch immediately above
it. Changes discovered during review are fixed on the branch where they were
introduced and then propagated forward before later work continues.

## Invariants across the stack

- Preserve callback FIFO order, timer ordering, cancellation semantics,
  `contextvars` capture, debug representations, exception routing, and
  thread-affinity checks.
- Keep Python callback invocation under the GIL. Release the GIL only while
  waiting for kernel work and never retain borrowed Python references across
  that wait.
- Use monotonic time and retain CPython's clock-resolution behavior.
- Bound callback execution per native tick so I/O, timers, signals, and
  cross-thread wakeups cannot starve one another.
- Keep CPython signal delivery through `signal.set_wakeup_fd`; do not make
  signalfd the default.
- Keep a Python/CFFI comparison path until the Phase 2 integration report is
  accepted.
- Do not move rare APIs such as DNS resolution into native code without a
  profile demonstrating value.

## Design clarification from Phase 1

The roadmap's broad eventfd wording is split into two wakeup mechanisms in the
detailed plan. Cross-thread scheduling uses eventfd. CPython signal delivery
keeps a byte-oriented nonblocking pipe/socket because `signal.set_wakeup_fd`
writes one byte per signal while Linux eventfd accepts only eight-byte counter
writes. This preserves the roadmap's CPython signal-semantics requirement
without relying on an incompatible descriptor protocol.

## Branch execution rule

Planning commits are the only changes made while opening this stack. When
implementation starts, work on one branch at a time. A branch is considered
complete only after its scoped tests, measurements, documentation, and quick
branch review pass. The dependent branch is then rebased or recreated from
that reviewed tip before its own implementation begins. No branch in this
stack is merged merely because its successor was opened.

## Validation and review policy

Every completed branch gets the lightweight branch-diff review before work
moves to its dependent branch. Native-memory, GIL, lifetime, or concurrency
changes additionally run the focused sanitizer and stress checks named in
that branch's plan. The final integration branch receives the broader Phase 2
review.

The common gate is:

1. Python 3.12 and 3.13 unit, compatibility, and e2e tests stay at or above
   the Phase 1 baseline.
1. Native and retained oracle paths pass the same relevant behavior tests.
1. Ruff, source distribution, wheel, and self-contained-liburing checks pass.
1. ASAN/UBSAN stays clean for native changes.
1. Benchmark claims retain raw samples and name the commit, Python, kernel,
   CPU, workload, concurrency, statistic, and noise.

## Phase 2 exit decision

Proceed to Phase 3 only if debug and non-debug behavior remain compatible and
the controlled scheduling benchmarks beat stock asyncio beyond their measured
noise floor. uvloop remains an external comparison target, not a mandatory
gate for every intermediate branch. If a native subcomponent has no measured
benefit, retain it only for a documented correctness, packaging, or ownership
advantage.
