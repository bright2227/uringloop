# Phase 3 UDP and pipe transports

Branch: `feature/phase3-datagram-pipes`

Depends on: `feature/phase3-readiness`.

## Deliverable and files

Port remaining common transports while preserving existing sendfile, TLS and
subprocess behavior. Touch native datagram/pipe records and operation helpers,
the private stub, loop `_make_datagram_transport`, `_make_read_pipe_transport`
and `_make_write_pipe_transport`, native proactor operation routing, UDP/pipe
tests and benchmark workloads. Split implementation internally into UDP then
pipes so their distinct message/stream contracts are independently testable.

## UDP sequence and tests

1. Lock connected/unconnected datagram APIs, IPv4/IPv6/Unix-address behavior
   and address ownership. Receive/send with owned sockaddr/msghdr/iovec storage
   when native operations require it; existing Python readiness fallbacks
   remain available until the native operation is validated.
2. Dispatch `datagram_received(data, addr)` without stream-style coalescing or
   send retry that duplicates a datagram. Preserve zero-length datagrams,
   message atomicity and explicit truncation behavior.
3. Preserve `error_received` semantics, connected-UDP refusal, sendto address
   validation, close/drain order and flow control. UDP loss is possible at
   the network level; no implementation silently drops locally owned messages
   to hide buffer exhaustion.
4. Test varying datagrams, empty/truncated receive, invalid/missing addresses,
   IPv6, connected errors, callback exceptions, reentrant send/close and
   capability fallback. Account sockaddr/exporter lifetime under cancellation.

## Pipe sequence and tests

1. Read pipes use native reads with bounded buffers and read-pause semantics.
   Write pipes preserve short-write ordering, drain behavior and pending-read
   hangup monitoring that the current `_IoUringWritePipeTransport` provides.
2. Track fd ownership, nonblocking state, EOF/BrokenPipe and exactly-once
   connection-lost. Closing the write transport while a HUP/error poll is
   racing must not dereference cleared state.
3. Test anonymous/FIFO pipes where supported, slow consumers, large writes,
   pause/resume, peer close, broken pipe, abort and loop shutdown. Re-run
   subprocess communicate/cancellation/output tests so pidfd child lifecycle
   and pipe lifecycle remain independent and correct.

## Preserved paths

CPython sslproto remains the TLS implementation. Check handshake success,
failed handshake timeout, shutdown timeout and EOF behavior through native
stream factories. No native TLS performance claim is introduced.

Retain sendfile behavior including the CFFI splice implementation and native
backend's current sendfile/readiness path. Native transports must either
support the required sendfile hooks or explicitly route to the compatible
fallback. Test count/offset, partial progress, resume normal writes,
cancellation and file position; do not label an os.sendfile fallback as splice.

## Measurement and exit

UDP round trips and pipe/subprocess throughput have separate counters,
latency/RSS and message sizes. Run Phase 1 file/sendfile regression workloads.
ASAN/UBSAN covers datagram metadata, pending pipe polls and shutdown; wheel
tests include any newly linked native helper.

Exit requires independent UDP and pipe contracts passing, preserved
sslproto/pidfd/sendfile tests, documented unsupported paths and no high/medium
lightweight review findings. TCP echo results cannot stand in for these gates.
