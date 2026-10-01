# Phase 2 native handle storage

Branch: `feature/phase2-native-handles`

Depends on: `feature/phase2-native-run-loop`

## Purpose

Reduce allocation and attribute-access overhead for hot `Handle` and
`TimerHandle` paths only after their compatibility surface and native loop
consumers are stable.

## Planned work

- Inventory the exact public and private attributes used by supported CPython
  versions, debug repr, exception reporting, tests, and representative
  libraries before choosing a native type boundary.
- Prefer native backing storage behind compatible Python-facing objects unless
  measurements justify replacing the public object types.
- Own callback, args, context, source traceback, and loop references with
  correct traversal, clearing, and deallocation.
- Preserve cancellation idempotence, timer comparison behavior, repr output,
  debug metadata, and exception-handler context.
- Evaluate freelists only after allocation profiles show a benefit; cap and
  clear any cache on interpreter/module teardown.
- Avoid reliance on undocumented struct layouts that differ between Python
  3.12 and 3.13.

## Design decision gate

Benchmark three choices before committing to a public native type:

1. Keep stock `asyncio.Handle` objects and optimize only queue storage.
1. Use a native backing record behind a Python-compatible wrapper.
1. Provide native heap types with the complete required Python surface.

Choose the least invasive option that produces a repeatable benefit. If queue
and tick work already removes the measured bottleneck, this branch may record a
no-go decision instead of adding permanent native types.

## Expected code changes if implementation proceeds

- Native handle/timer storage with traverse, clear, dealloc, repr, getters,
  cancellation, comparison, and callback-run support in `_uringcore`.
- Typed private interfaces in `_uringcore.pyi` and an internal ABI bump.
- Loop factories in `uringloop/loop.py` that create the selected handle form
  while preserving supported asyncio-visible behavior.
- Compatibility tests expanded before the default type changes; allocation
  benchmarks retain results for all evaluated designs.
- Module-state cleanup for any bounded freelist, with subinterpreter-safe
  ownership rather than process-global Python references.

## Delivery sequence

1. Inventory attribute access in uringloop, CPython tests, debug/error paths,
   and representative downstream libraries.
1. Record allocation profiles after the native run-loop branch.
1. Prototype the backing-record option and full-native option behind an
   experimental selector; do not ship both permanently.
1. Run compatibility, GC, sanitizer, and allocation benchmarks and write the
   design decision into this document.
1. Integrate only the selected design and remove prototype-only code.

## Compatibility and safety rules

- Preserve callback argument tuple and `contextvars.Context` identity/lifetime.
- Cancellation clears callback/args only where CPython does and remains
  idempotent.
- Repr and exception context must not dereference partially cleared state.
- Timer comparisons with unrelated types return the compatible result rather
  than reading foreign memory.
- A freelist never retains callback graphs, is strictly bounded, and is cleared
  when module/interpreter state is finalized.

## Required tests

- Contract tests for attributes, repr, equality/ordering, cancellation,
  contexts, traceback metadata, and exception reporting.
- Reference-lifetime, cyclic-GC, interpreter-shutdown, allocation-failure, and
  repeated create/cancel/drain stress.
- Differential tests against stock asyncio handles for supported behavior.
- Allocation counts and scheduling benchmarks with and without native backing.

## Non-goals

- No public stable ABI promise, cross-interpreter handle sharing, unbounded
  object cache, or private CPython struct embedding.

## Exit criteria

Compatibility remains at or above the locked baseline, native ownership is
sanitizer-clean, and the retained design has a measured allocation or dispatch
benefit.

The lightweight review checks the design evidence first, then API compatibility,
GC/module-state ownership, failure cleanup, stub/ABI agreement, and removal of
the rejected prototype.

## Compatibility inventory

The Python 3.12 and 3.13 implementations expose the same object layout and
core behavior that this project consumes:

| Surface | Required behavior and consumers |
| --- | --- |
| `_callback`, `_args`, `_context`, `_loop` | Strong ownership from construction through dispatch or cancellation; `Context` identity is visible through `get_context()`. |
| `_cancelled`, `cancel()`, `cancelled()` | Idempotent cancellation; callback and args are cleared once, while debug mode caches a safe repr first. The native ready/timer paths read `_cancelled`. |
| `_source_traceback`, `_repr`, `_repr_info()`, `__repr__()` | Debug creation location and slow-callback/error formatting must remain valid after cancellation. The loop trims the timer creation frame. |
| `_run()` | Calls through the captured context, propagates `SystemExit`/`KeyboardInterrupt`, and routes other exceptions with `message`, `exception`, `handle`, and optional `source_traceback`. |
| `__weakref__` and cyclic GC | Handles may participate in callback/loop cycles and must remain weak-referenceable. |
| `_when`, `_scheduled`, `when()` | Timer deadline remains readable; cancellation notifies the loop, and native timer promotion writes `_scheduled = False`. |
| timer hash/comparisons | Hash follows `_when`; comparisons accept compatible `TimerHandle` instances and return `NotImplemented` for unrelated types. Equality also observes callback, args, and cancellation. |

Python 3.13 additionally passes the loop's debug flag to
`_format_callback_source`; Python 3.12 does not support that keyword. A native
replacement would therefore need a version adapter rather than embedding or
copying either version's private struct layout. Existing compatibility and
scheduling-contract matrices already cover FIFO snapshots, contexts,
cancellation, debug repr, exception context, and timer behavior for every
retained loop backend.

## Implemented design gate

`benchmarks.native_handles` evaluates the three planned choices with 20,000
plain handles and 20,000 timer handles per smoke workload:

1. stock `asyncio.Handle` and `asyncio.TimerHandle`;
1. a Python-facing wrapper around a native backing record; and
1. benchmark-only native heap-type prototypes.

Each sample measures construction, real `LoopCore` ready-queue or timer-heap
storage, context-aware callback dispatch, repeated cancellation, traced
allocation counts, traced peak bytes, and retained shallow size. Identity,
FIFO order, callback completion, and cancellation counts are self-checked.
The C prototypes use GC traversal/clear/deallocation and strong reference
ownership, and their lifetime/cycle tests run under ASAN/UBSAN. They are built
only when `URINGLOOP_BUILD_HANDLE_PROTOTYPES=1`; they are not in `_uringcore`,
its stubs, its ABI, or default wheels.

## Captured design-gate baseline

`benchmarks/results/phase2-native-handles-2026-08-10-wsl2.json` was captured
from harness commit `108c8cb` on Python 3.12.3 / WSL2 with one warmup and five
measured runs. Rates and sizes below are medians; CV is for complete-sample wall
time.

| Kind | Choice | Wall (s) | CV | Allocate/s | Native storage/s | Dispatch/s | Shallow B/object | Blocks/object |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Handle | stock Python | 0.0852 | 2.2% | 516,304 | 1,379,750 | 746,020 | 104 | 2.99 |
| Handle | native record wrapper | 0.1160 | 2.5% | 381,580 | 484,402 | 1,112,282 | 144 | 3.99 |
| Handle | full native prototype | 0.1257 | 1.0% | 558,846 | 1,353,630 | 277,026 | 96 | 2.99 |
| Timer | stock Python | 0.2343 | 0.9% | 349,375 | 148,067 | 742,247 | 120 | 3.99 |
| Timer | native record wrapper | 0.2888 | 3.8% | 279,415 | 121,247 | 1,052,677 | 160 | 4.99 |
| Timer | full native prototype | 0.3376 | 1.6% | 490,430 | 147,933 | 267,706 | 112 | 2.99 |

The full-native prototype allocates plain handles about 8% faster and timers
about 40% faster than stock, with 8 bytes less shallow storage per object.
Those local wins do not survive the complete measured path: stock handles are
about 36% faster than the wrapper and 48% faster than the full-native prototype
in total wall time; stock timers are about 23% and 44% faster respectively.
The wrapper also adds one allocation and 40 shallow bytes per object. These are
isolated design-gate results, not application-speed claims.

## Decision: retain stock handle types

This branch records a no-go for permanent native handle types. The stock types
win the complete measured path with low variation, while already preserving
the exact version-specific repr, exception, comparison, context, and GC
contracts. The full-native prototype implements less compatibility behavior
than a shippable type yet is still slower overall; completing that surface or
adding a freelist would add ownership and interpreter-finalization risk without
evidence of an end-to-end benefit.

Accordingly, `IoUringProactorEventLoop` continues to construct stock asyncio
handles, `_uringcore.ABI_VERSION` remains 12, no handle selector or freelist is
added, and the CFFI/native oracle matrix is unchanged. Rejected prototypes are
absent from production code and retained only as an opt-in, benchmark-package
fixture so the decision remains reproducible. A future branch may reopen the
gate only with a profile showing handle construction as an end-to-end
bottleneck and a prototype that beats the complete path beyond noise.
