# Phase 1 native ring backend decision

**Status:** accepted\
**Date:** 2026-08-01

## Decision

Use the pinned, vendored liburing as the implementation layer for the native
ring and link it statically into the extension. The production module will use
the canonical `_uringcore` name; `_uringcore_liburing` is only the
comparison-spike name.

The raw-syscall spike is preserved on the
`feature/raw-syscall-ring-spike` branch for future reference, but it is not
built or packaged by the selected implementation branch. The project will not
maintain both implementations in production.

## Context

Phase 1 required lifecycle spikes for both viable implementation routes:

- direct `io_uring_setup` and `mmap` calls; and
- a statically linked, vendored liburing.

Both spikes expose the same `Ring` construction, introspection, deterministic
close, context-manager, and deallocation behavior. Neither submits or reaps
requests, so the measurements below compare only the lifecycle boundary. In
particular, they cannot establish submit/reap throughput.

## Measurements

Measurements were taken from commit `467543c` on CPython 3.12.3, x86-64,
GCC 13.3.0, GNU binutils 2.42, and Linux
6.18.33.2-microsoft-standard-WSL2. The pinned liburing revision was
`f4e42a515cd78c8c9cac2be14222834be5f8df2b` (liburing 2.5).

### Correctness boundary

The two extensions have matching tests for:

- queue-size validation;
- kernel-reported SQ/CQ sizes and features;
- deterministic, idempotent `close()`;
- context-manager cleanup and closed-ring rejection; and
- equivalent public ABI-version reporting.

This demonstrates equivalent behavior at the spike boundary, not equivalence
for request submission, completion, cancellation, or shared-ring ordering.

### Wheel contribution

`uv build --wheel` used the interpreter's normal extension flags, including
`-O2 -g`; the wheel was not stripped. The table reports each wheel member,
not an estimated whole wheel containing only that backend.

| Route | Wheel member, unpacked | Wheel member, compressed | `strip --strip-unneeded` copy |
| --- | ---: | ---: | ---: |
| Raw syscalls | 35,152 B | 13,099 B | 15,536 B |
| Static liburing | 109,840 B | 45,476 B | 27,792 B |
| Static-liburing increase | 74,688 B | 32,377 B | 12,256 B |

The complete comparison wheel, which contains both backends and the existing
CFFI extension, was 170,228 bytes compressed. The static extension's dynamic
section lists only `libc.so.6`; it does not require a system `liburing.so` at
runtime.

The size cost is acceptable for avoiding a substantially larger
correctness-sensitive implementation. Release manylinux and musllinux wheel
sizes must still be recorded when those builds exist.

### Lifecycle syscalls

`strace -c` around 1,000 construct/close iterations reported identical calls
for both routes:

| Syscall | Raw syscalls | Static liburing |
| --- | ---: | ---: |
| `io_uring_setup` | 1,000 | 1,000 |
| `mmap` | 2,080 | 2,080 |
| `munmap` | 2,008 | 2,008 |
| `close` | 1,147 | 1,147 |

The totals include interpreter startup and imports, but those totals are the
same for both processes. Neither spike calls `io_uring_enter`. Repeated timing
on WSL2 varied by more than 4x within each route, so the observed lifecycle
timings are not used to select a backend.

Submission and completion benchmarks remain mandatory once the native batch
API exists. liburing helpers are largely inline, but that is not evidence that
their hot-path cost is zero.

## Tradeoff comparison

| Criterion | Raw syscalls | Static, vendored liburing |
| --- | --- | --- |
| Wheel size | Smaller | About 32 KB more compressed in this comparison build |
| Runtime dependency | None beyond libc and the kernel ABI | None beyond libc and the kernel ABI |
| Build complexity | Ordinary extension build | Must build and statically link the pinned submodule |
| Source packaging | Self-contained | Self-contained through the vendored-source build hook |
| License work | No additional bundled-library notice | MIT notice must remain in binary/source distributions |
| Sanitizers | Extension flags cover all project-owned ring code | liburing must also be rebuilt with matching sanitizer flags |
| Lifecycle syscall count | Identical | Identical |
| Unsafe surface | Project owns mappings and all future shared-ring ordering | liburing owns mappings and established SQ/CQ ordering helpers |
| Maintenance | Must track kernel ABI details directly | Must update and test a pinned upstream dependency |

The current raw spike is 344 C lines, compared with 256 C lines for the
liburing wrapper. The more important difference is future code: the raw route
would make this project responsible for acquire/release ordering of shared SQ
and CQ heads and tails, ring wrapping, feature-specific layouts, and upstream
kernel ABI evolution. Sanitizers do not prove that this concurrency protocol
is correct.

liburing already centralizes those rules and is the same abstraction used by
the existing CFFI implementation. Its build and attribution costs are
concrete and bounded. The extra wheel size is small compared with the
correctness and maintenance risk removed from the Phase 1 request core.

## Required follow-up

Selecting liburing does not declare the current spike production-ready. The
following work blocks that transition:

1. Build liburing itself with ASAN/UBSAN flags in sanitizer jobs, then run the
   native e2e suite against that instrumented archive.
1. Move the selected implementation behind the canonical `_uringcore` name
   and replace the comparison-spike module name, stub, tests, and build
   configuration.
1. Implement and benchmark the native prepare/submit/reap batch boundary
   before making claims about syscall or CPU improvements.
1. Continue with the refcounted multi-completion request state machine only
   after the selected ring backend is packaged and sanitizer-clean.

The pure-Python/CFFI implementation remains the behavioral oracle throughout
this work.

## Packaging follow-up

The first required follow-up was completed by teaching `build_ext` to copy,
configure, and compile the pinned liburing sources in its private build
directory. The sdist includes only the source, internal headers, and build
metadata needed for that archive. Both extension modules link the resulting
archive, so a clean source build neither consumes a checkout artifact nor
links a system `liburing.so`.
