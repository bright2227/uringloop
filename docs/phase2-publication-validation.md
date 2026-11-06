# Phase 2 publication validation — 2026-11-06

## CI fixes and current validation

The original failures below are resolved. CPython's Unix signal registration
and byte-oriented self-socket dispatch now work with socketpair, eventfd, and
the integrated Phase 2 loop. Closing the loop restores registered handlers.
The sanitizer rebuild explicitly supplies its setuptools build dependency.

New, dated timer-heap and native-handle captures replace the missing inputs
in the current report harness; the August artifacts and index are preserved.
Tests verify their provenance, schema and report arithmetic without requiring
a speedup. See `RECOVERY.md` for the historical gaps and new input paths.

The complete local Python 3.12 suite passes: **372 passed, 1 skipped**, with
the optional handle benchmark prototypes built. Ruff and the CPython source
verification/compatibility report pass; the compatibility report has 104
passing cases. Controlled integrated performance approval remains open.

## Initial publication checkpoint

The publication branch includes recovered Phase 2 tip `f174269` and its
unmerged Phase 1 prerequisites, with remote `main` at `c18dc43` merged in.
The dependency security constraint and locked cryptography update are retained.
The original recovery checkout and unrelated local changes are preserved.

The native and CFFI extensions build on CPython 3.12.3. Ruff and
`uv lock --check --offline` pass. Pytest explicitly includes the repository
root on its import path so the benchmark helpers are importable through both
the pytest entry point and `python -m pytest`. CI accepts stacked PR bases.

Kernel and socket tests require execution outside the managed sandbox. The
following run used that permission and stopped after three failures:

```bash
.venv/bin/python -m pytest -q --tb=short --maxfail=3 tests/
```

Result: **341 passed, 7 skipped, 3 failed**; remaining tests were not run.
The failures are:

- `test_eventfd_thread_wakeup_keeps_signal_socketpair_active`:
  `add_signal_handler` raises `NotImplementedError` on the proactor loop.
- `test_phase2_integration_index_reconciles_committed_artifacts`: the historical
  timer-heap raw JSON is missing.
- `test_captured_handle_design_gate_is_complete_and_tied_to_harness`: the
  historical native-handle raw JSON is missing.

The two artifact gaps are documented in `RECOVERY.md`. Their samples have not
been reconstructed or replaced by synthetic data. The focused Phase 2 suite
also ran: 100 passed, 6 skipped, and the same two artifact checks failed.

This is a draft publication for review, not a passing full-suite result or
approval to enable Phase 2 by default. Signal support, auditable replacement
benchmark evidence, and controlled integrated performance validation remain
open.
