# Phase 3 multishot accept

Branch: `feature/phase3-multishot-accept`

Depends on: `feature/phase3-send-zc`.

## Deliverable and files

Expose native one-shot/multishot accept requests and integrate repeated accept
with TCP/Unix servers. Unlike receive/SEND_ZC, this checkout does not already
expose a native accept method. Touch native request kinds/preparation and
completion routing, `uringloop/_uringcore.pyi`, native proactor listener
registration, loop `_start_serving`/`_stop_serving`, server tests and benchmark
connection-churn mode.

## Sequence and ownership

1. Add native accept with validated listener fd, flags and owned address
   storage. Use nonblocking/close-on-exec accepted descriptors. Multishot
   flags are probe/operation-result gated; one-shot rearm remains available.
2. Transfer each successful accepted fd from CQE ownership to socket then
   transport as a transaction. Any allocation, socket wrapping, peer-address
   retrieval, protocol factory or connection-made failure closes exactly the
   owned fd and restores server counts.
3. Keep listener request alive through all MORE completions. On final CQE,
   rearm only for the active listener generation. Bound accepted deliveries
   per tick and retained not-yet-attached sockets.
4. Closing/stopping a server stops new connection dispatch, cancels accept,
   and closes successful late accepts that won the race. Listener fd reuse
   cannot attach an old accepted connection to a new server.
5. Handle transient aborted accepts, capacity errors and EMFILE/ENFILE using
   compatible reporting and paced retry; avoid a tight accept-error loop.
   Bound deferred accept work and keep existing backlog/start-serving rules.

## Tests and measurement

TCP IPv4/IPv6 and Unix servers, concurrent connect/close, start-serving false,
server wait-closed, factory failure, accepted-fd allocation failure, stop in
connection-made, final MORE-cleared success, late success after cancellation,
fd reuse and file-descriptor exhaustion. TLS connections must still wrap
through sslproto. ASAN/UBSAN and fd-count stress cover shutdown/rapid restart.

Benchmark connection churn separately from established-stream echo: accept
SQEs/syscalls per connection, connections/sec, connection latency, CPU and
peak pending sockets. Exit requires no fd/resource leaks, tested one-shot
fallback and lightweight review clear of high/medium findings. Publishing an
accept win does not establish steady-state transport throughput parity.
