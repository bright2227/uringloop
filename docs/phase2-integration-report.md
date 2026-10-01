# Phase 2 integration and benchmark report

Branch: `feature/phase2-integration-report`

Depends on: `feature/phase2-native-handles`

## Purpose

Integrate the completed loop-core components, select the default path, and
produce the correctness and performance evidence for the Phase 2 exit decision.

## Planned work

- Exercise the native queue, timers, wakeups, tick, and handle storage together
  behind an explicit opt-in before changing the default loop path.
- Run the same scheduling, compatibility, and e2e suites against the integrated
  path and retained oracle; document every deliberate difference or skip.
- Stress close/restart, callback and timer storms, mixed socket/file I/O,
  signals, subprocesses, and cross-thread producers.
- Run ASAN/UBSAN, Python 3.12/3.13, wheel/sdist, self-contained-liburing, and
  clean-install checks.
- Run the controlled scheduling matrix against Phase 1, stock asyncio, and
  pinned uvloop; retain raw JSON and quantify noise before percentage claims.
- Update the roadmap and publish a Phase 2 report with a clear go/no-go decision
  for native transports.

## Integration sequence

1. Rebase/propagate every reviewed predecessor fix so the stack is linear and
   each branch review applies to the code actually integrated.
1. Add one explicit experimental loop-core selector and run the full matrix
   against the retained Phase 1 path before changing defaults.
1. Resolve integration-only lifetime, close-order, signal, subprocess, and
   fairness issues with focused regressions.
1. Switch the public loop to the accepted Phase 2 core while keeping a private
   oracle selector for differential tests.
1. Run release-style correctness, sanitizer, packaging, and controlled
   performance gates from committed revisions.
1. Commit raw results, update roadmap status, and record the Phase 3 decision.

## Required validation matrix

- Python: 3.12 and 3.13 locally/CI, with any additional version clearly marked
  informational until officially supported.
- Modes: debug and non-debug; Phase 2 default and retained oracle.
- Workloads: loop contracts, full unit/e2e, callback/timer storms, mixed I/O,
  signals, subprocesses, cross-thread scheduling, and close/restart stress.
- Native safety: ASAN/UBSAN e2e plus repeated GC/lifetime stress.
- Distribution: sdist contents, CPython/liburing attribution, Linux wheel
  import, no dynamic system liburing, and clean-environment execution.
- Performance: ready batches, timers, mixed ticks, cross-thread wakeups, Phase 1
  socket/streams/file regressions, stock asyncio, and pinned uvloop.

## Report contents

- Exact code and harness commits plus raw JSON artifacts.
- Machine/kernel/Python/backend/workload metadata and warmup/repeat counts.
- Median CPU/throughput, p50/p99 latency, RSS, syscall counts, CV, and confidence
  limits or another stated uncertainty method.
- Separate correctness, packaging/safety, and performance conclusions.
- Regressions and no-go decisions, not only wins.
- Remaining oracle support, known limitations, and concrete Phase 3 inputs.

## Rollback rules

- A component that fails compatibility or native-safety gates returns to its
  predecessor implementation before default integration.
- A component with no repeatable speed benefit is removed unless its documented
  ownership, packaging, or maintenance benefit independently justifies it.
- Do not hide unavailable io_uring or eventfd support behind an implicit stock
  asyncio fallback.

## Exit criteria

- Behavior and debug compatibility are at or above the Phase 1 baseline.
- The integrated default has no unresolved high- or medium-severity review
  findings and all branch-level lightweight reviews are recorded as complete.
- At least one named scheduling hot path beats stock asyncio beyond the
  controlled baseline's noise, without a material regression in the other
  published workloads.
- The report states whether to proceed to Phase 3 and which Python/oracle paths
  remain supported during that phase.

## Final review checklist

- All lightweight predecessor reviews are complete and their fixes propagated.
- No unresolved high/medium correctness, ownership, GIL, signal, or packaging
  findings remain.
- Published arithmetic can be reproduced from committed raw artifacts.
- The current branch is not merged as part of this planning or report step.

## Integrated selector

`IoUringProactorEventLoop(loop_core="phase2")` now selects the complete
reviewed stack in one explicit operation: native ready storage, native timer
storage, eventfd cross-thread wakeups, and the bounded native tick. The
selector rejects component-level overrides so a named integrated mode cannot
silently become a hybrid. `loop_core="phase1"` names the retained Python
scheduling/socketpair oracle, while the default and the existing granular
arguments remain backward compatible.

The integrated selector remains experimental and is not the public default.
It raises the underlying setup error if io_uring or eventfd is unavailable;
there is no implicit selector-loop fallback. Stock asyncio `Handle` and
`TimerHandle` objects remain shared by both paths following the reviewed
native-handles no-go decision.

## Reviewed branch lineage

| Branch | Reviewed tip | Result |
| --- | --- | --- |
| `feature/phase2-loop-contracts` | `215059a` | pass |
| `feature/phase2-native-ready-queue` | `76a1bcc` | pass after zero-limit batch fix |
| `feature/phase2-native-timer-heap` | `1a3cb22` | pass after reentrancy guards/tests |
| `feature/phase2-eventfd-wakeup` | `1a9e5eb` | pass after poll-failure fallback fix |
| `feature/phase2-native-run-loop` | `396c092` | pass after finite-bound and benchmark-budget fixes |
| `feature/phase2-native-handles` | `a383f76` | pass; retain stock handles |

Every branch was created from the reviewed tip immediately above it. No branch
was merged or pushed while producing this stack.

## Integration validation

The committed non-kernel integration tests construct the Phase 2 selector with
an in-memory proactor and exercise all four components together. They cover a
10,000-item mixed ready/timer storm across multiple fairness-budget ticks,
stop/restart snapshot ordering, cross-thread publication/eventfd notification,
close ordering, selector validation, and the retained Phase 1 mode. Focused
Python 3.12 checks pass, as do Python 3.13 direct selector/stress checks.
Branch-level native ownership checks remain ASAN/UBSAN-clean, including the
integrated storage/tick tests and the benchmark-only handle prototypes.

The default sdist and CPython 3.12 wheel build and pass the self-contained
liburing verifier; the opt-in handle prototype module is absent from the
wheel. Ruff and raw-artifact provenance checks pass. Before the sandbox policy
changed, the Phase 2 base passed the full Python 3.12/3.13 suites; each later
component then passed its applicable focused 3.12/3.13 and branch-level tests.
In the current managed sandbox, broad reruns stop at `EPERM` from
`io_uring_setup` and network socket creation. Those unavailable gates are
recorded, not converted to skips or silent fallbacks.

The scheduling benchmark matrix now includes six isolated backends:
`native-phase2`, retained `native`, `cffi-phase2`, retained `cffi`, stock
asyncio, and pinned uvloop. It is ready for the existing controlled workflow,
but no local integrated result is fabricated when the kernel syscalls are
denied.

## Auditable evidence index

`benchmarks/results/phase2-integration-2026-08-10-wsl2.json` was generated
from report harness commit `d3aefdb`. It records every reviewed predecessor
tip, SHA-256 and harness commit for all six raw Phase 2 inputs, the 24-result
contract baseline with syscall coverage, and derived arithmetic. The index is
reproducible with:

```bash
python -m scripts.phase2_integration_report \
  --output benchmarks/results/phase2-integration-2026-08-10-wsl2.json
```

The derived smoke results are:

| Component | Isolated median change | CV / limitation | Decision |
| --- | ---: | --- | --- |
| Native ready storage | +32.6% direct; -3.4% through the loop adapter | loop CV 3.9% native, 6.1% Python | experimental only |
| Native timer heap | +126.1% lifecycle throughput | CV 10.4% native, 20.8% Python | named isolated win |
| Native tick state plumbing | +55.1% | CV 48.7% native, 14.9% Python | functional evidence only |
| Eventfd wakeup | no comparison | socketpair blocked locally; eventfd p50/p99 30.2/67.3 us | no speed claim |
| Full-native handle prototype | +47.6% handle and +44.1% timer wall time (regressions) | CV 1.0% and 1.6% | rejected; stock retained |

Positive percentages in the first three rows mean higher throughput. Positive
native-handle wall-time percentages are regressions. These component results
do not substitute for the six-backend integrated matrix.

## Phase 2 decision

Phase 2 implementation and branch-level review are complete, but default
integration is a no-go at this checkpoint. The timer heap is a clear isolated
hot-path win; ready-loop dispatch shows no gain, eventfd lacks a local
comparison, the tick smoke run is too noisy, and native handles regress the
complete measured path. Without a controlled integrated run, there is no
evidence that enabling the whole stack avoids material socket, stream, file,
latency, or syscall regressions.

Therefore:

- the public default remains the Phase 1 path;
- `loop_core="phase2"` is retained for explicit testing and controlled runs;
- native/CFFI Phase 1 and Phase 2 backends remain in the differential matrix;
- no native handles or freelist ship; and
- Phase 3 native transports are not approved until the six-backend controlled
  matrix passes the stated exit gate.

The next approval step is operational rather than another implementation
guess: run the committed full-profile workflow on an io_uring-capable pinned
machine, review correctness and noise-adjusted regressions, then either change
the default in a new branch or roll back the losing Phase 2 components.
