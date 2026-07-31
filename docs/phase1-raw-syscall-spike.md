# Phase 1 raw-syscall ring spike

This is the first of the two native ring implementation spikes required by
Phase 1 of the roadmap. It establishes an importable `_uringcore` extension
whose `Ring` type owns:

- the file descriptor returned by `io_uring_setup`;
- the submission and completion queue mappings; and
- the submission queue entry mapping.

`Ring.close()` releases those resources deterministically and is idempotent.
Deallocation provides the same cleanup as a fallback.

The spike intentionally does not submit or reap operations and is not wired
into the Python proactor. The CFFI implementation remains the behavioral
oracle while the native API is developed.

The extension calls the kernel ABI directly and does not link to liburing.
The follow-up static-liburing spike implemented the same lifecycle boundary.
The resulting [backend decision](phase1-native-ring-decision.md) selects the
statically linked, vendored liburing route. This raw implementation remains
temporary comparison code until the selected backend moves to the canonical
`_uringcore` module.
