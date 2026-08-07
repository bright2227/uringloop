# Phase 1 native operations

The native core owns the complete kernel-facing lifetime for socket send and
receive, file read and write, multishot receive, timeout, cancellation, and
zero-copy send requests.

Caller-provided buffers are acquired through Python's buffer protocol before
an SQE is reserved. The request retains both the exporter and the file object
until its completion model reaches the final CQE. Writable operations reject
read-only exporters, non-contiguous buffers are rejected, and lengths that do
not fit the io_uring ABI are rejected before submission.

Multishot receive selects from the bounded provided-buffer pool. Each CQE owns
an immutable `ProvidedBuffer` object. `to_bytes()` documents and performs a
copy; `memoryview(buffer)` exports the native slot without copying. A slot
cannot return to the kernel while a view is exported, and the ring cannot close
while any selected slot remains consumer-owned. Pool exhaustion terminates the
multishot request with `-ENOBUFS`; after consumers release slots, callers can
re-arm the operation without allocating more memory.

`SEND_ZC` requests use the notification completion model. The operation CQE
publishes the send result, but the Python buffer export remains active while
`IORING_CQE_F_MORE` indicates a notification will follow. Only the
`IORING_CQE_F_NOTIF` CQE releases the exporter. This rule also applies when the
operation result is an error.
