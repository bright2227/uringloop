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
before building this experimental extension; packaging the vendored sources
for standalone wheel builds remains part of the route decision.

The decision record can now compare the two lifecycle implementations using
the same API and tests. Neither spike is the production backend until that
record selects a route.

On the initial CPython 3.12 x86-64 development build, including debug
information, the module sizes are:

| Route | Extension size |
| --- | ---: |
| Raw syscalls | 35,152 bytes |
| Static liburing | 109,840 bytes |

The static module adds 74,688 bytes in this build. These are spike
measurements rather than release-wheel results; the decision record must
repeat them with the release build and record its compiler and strip
settings.
