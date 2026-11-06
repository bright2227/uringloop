# Phase 1 completion queues and runtime probing

The native ring requests an explicit completion-queue size with
`IORING_SETUP_CQSIZE`. By default the requested CQ is twice the SQ size, and a
caller may request a larger value with `cq_entries`. The kernel-reported sizes
remain available as `sq_entries` and `cq_entries`.

The ring exposes three pressure signals:

- `cq_ready` is the number of CQEs immediately available;
- `cq_overflowed` reports the kernel's slower overflow path; and
- `cq_dropped` reports unrecoverable CQE loss on kernels without a usable
  no-drop path.

`reap()` checks the dropped counter before and after every batch and raises a
`BufferError` rather than silently losing ownership events. Callers must keep
reaping while `cq_overflowed` is true so the kernel can flush overflowed CQEs
back to the mapped CQ.

At construction, the native core calls `io_uring_get_probe_ring()` and exposes
the result as the immutable `supported_opcodes` set. Every operation checks its
opcode before reserving an SQE. Tests may inject `supported_opcodes` into the
constructor to cover unsupported fallbacks independently of the host kernel.

Registered ring fds remain disabled: Phase 1 requires probing and measurement
to justify them, and the current benchmark evidence does not establish a
benefit. This avoids adding registration teardown state before it has a
measured use.
