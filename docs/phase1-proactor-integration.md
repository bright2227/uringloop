# Phase 1 proactor integration

The public `IoUringProactor` now uses `_uringcore.Ring` as its submission and
completion engine. Socket receive and send, file read and write, cancellation,
and readiness polling cross the Python/native boundary once when prepared and
once per completion batch. `Ring.submit()` drains every prepared SQE even when
the kernel accepts only a partial batch, and `Ring.wait()` combines the wait
and reap boundary used by the event-loop tick.

The wait releases the GIL while the kernel blocks, so another Python thread
can write the event-loop self-pipe through `call_soon_threadsafe`. Concurrent
close, reinitialization, submission, and reaping are rejected while that raw
wait is active. Ring setup flags accepted by the former public constructor
continue to be passed to `io_uring_queue_init_params`.

The former CFFI proactor remains private as `_CffiIoUringProactor`. It is kept
as a behavioral oracle for focused differential tests during the native-loop
transition; it is no longer the implementation exported by the package.

Phase 1 deliberately keeps rare or structurally different APIs in Python.
Accept, connect, UDP, and sendfile use native `POLL_ADD` readiness requests and
then perform the corresponding nonblocking Python syscall. This preserves the
existing proactor contract without expanding the request ABI immediately
before Phase 2. The native ring still owns the polling request and its file
reference, so cancellation and shutdown use the same request state machine.

The compatibility paths are explicit implementation choices, not a fallback
to a selector event loop. Unsupported required io_uring opcodes fail during
request preparation with `NotImplementedError` based on the runtime probe.

All native Python owners participate in cyclic GC. If an unreachable file
wrapper points back to its ring, GC closes the ring before releasing the
request's exported buffers and Python references. Context-manager exit uses
the same owned-provided-buffer check as explicit `close()`.
