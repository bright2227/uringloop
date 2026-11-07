# Phase numbering and development priorities

The authoritative current plan is [ROADMAP.md](../ROADMAP.md). Its order was
updated on 2026-11-07 to put a small, correct, usable asyncio backend before
optional native scheduling and transport rewrites.

## Current order and historical mapping

| Current phase | Purpose | Previous phase |
|---|---|---|
| 0 | Foundations: tests and reproducible benchmarks | 0 |
| 1 | Native ring/request core with Python asyncio plumbing | 1 |
| 2 | Correctness and asyncio compatibility | 4 |
| 3 | Performance measurement and targeted tuning | 5 |
| 4 | Packaging, documentation, and releases | 6 |
| 5 | Optional native loop core and scheduling | 2 |
| 6 | Optional native transports and protocol dispatch | 3 |

Phase 1 follows the same broad division as CPython's Windows proactor: native
I/O primitives and resource ownership, with scheduling, proactor orchestration,
and transports in Python. Moving those Python layers into C is a separate
optional performance decision in Phases 5 and 6.

The core product can ship after its baseline release gates pass without
implementing either optional phase. Phase 3 requires useful measurements and
documented decisions, not a speedup from every proposed optimization. Optional
experiments still require correctness, ownership, and sanitizer coverage for
every path retained or shipped.

## Repository status after the revert

Remote `main` at `adb0a7e` includes PR #25's revert of PR #23. That revert
removed later native request/proactor prerequisites as well as the scheduling
experiment. The early `_uringcore` lifecycle/NOP foundation remains; a
complete Phase 1 native I/O backend must not be inferred from the old phase
reports or from the earlier merge of #23.

[PR #24](https://github.com/bright2227/uringloop/pull/24) remains open as
experimental native transport work, now classified under optional Phase 6.
It was built on the reverted prerequisites and needs its dependencies
reconciled before any future integration. Branch validation recorded before
the revert does not establish merge readiness against the reverted `main`.

Correctness and resource-safety fixes developed in historical experiments
belong in the baseline where applicable and can be reviewed separately from
native scheduling or transport rewrites. Renumbering does not restore
reverted implementation or promote an experiment into the default path.

## Historical identifiers

Existing branch names, document paths, API values, benchmark backend names,
raw artifacts, and dated reports keep their original identifiers for
compatibility and traceability. For example:

- `feature/phase2-*` and `docs/phase2-*.md` in historical work describe the
  native scheduling experiment, now optional Phase 5.
- `feature/phase3-*` and `docs/phase3-*.md` in historical work describe native
  transports, now optional Phase 6.
- An experimental `loop_core="phase2"` selector refers to the native loop
  experiment; it does not mean current Phase 2 compatibility work.

These examples identify historical or experimental interfaces, not APIs
currently available on the reverted `main`. When that work is revisited,
current planning should use this mapping while preserving recorded evidence
and literal API names. Old approvals to proceed to "Phase 2" or "Phase 3"
describe the former plan and do not override today's optional-phase gates.
