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
