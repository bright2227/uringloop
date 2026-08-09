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
