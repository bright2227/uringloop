# Phase 1 static-liburing ring spike

This is the second native ring implementation spike required by Phase 1 of
the roadmap. It mirrors the lifecycle boundary of the raw-syscall
`_uringcore.Ring` with a separate `_uringcore_liburing.Ring` implemented
through the pinned liburing submodule.

The extension links `libs/src/liburing.a` into the module. It therefore has
no runtime dependency on a system `liburing.so`. liburing is used under its
MIT license, whose notice is included in the package.

Like the raw-syscall spike, this module owns ring initialization and teardown
but does not submit or reap operations and is not wired into the Python
proactor. The source checkout must configure and build the pinned submodule
before building this experimental extension. Packaging the vendored sources
for standalone wheel builds remains required follow-up.

The two lifecycle implementations use the same API and tests. The resulting
[backend decision](phase1-native-ring-decision.md) selects the statically
linked, vendored liburing route. This spike is not yet the production backend:
the follow-up work must make source builds self-contained, add full sanitizer
coverage, move this implementation to `_uringcore`, and remove the raw spike.

On the initial CPython 3.12 x86-64 development build, including debug
information, the module sizes are:

| Route | Extension size |
| --- | ---: |
| Raw syscalls | 35,152 bytes |
| Static liburing | 109,840 bytes |

The static module adds 74,688 bytes in this build. See the backend decision
for compressed wheel-member sizes, stripped sizes, the measurement
environment, and the other selection criteria.
