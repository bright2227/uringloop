# Phase 3 stream-write correctness prerequisite

Branch: `feature/phase3-stream-write-correctness`

Depends on: `feature/phase3-transport-contracts`.

## Confirmed problem and deliverable

Both uringloop proactors return `Future[int]` from send operations, where the
integer can be a partial send count. The inherited CPython proactor transport
write path assumes its send Future covers the entire supplied buffer and
does not advance by that integer. Clearing the pending buffer after a short
send can therefore lose bytes. Correct this before evaluating native stream
optimizations or comparing throughput; a fast corrupted stream is not useful
benchmark evidence.

The initial fix uses a Python `_PartialWriteMixin` and an uringloop socket
transport subclass retaining CPython's read/lifecycle implementation. Apply
the same counted-send behavior to write pipes. This is a transport safety
fix; do not label it as a fully native transport or a completed Phase 3 port.

## Touch points and implementation

- `uringloop/stream_transport.py`: counted-send mixin and socket transport.
- `uringloop/loop.py`: socket/write-pipe transport
  subclasses and `_make_socket_transport` selection for TCP/Unix/TLS plumbing.
- `tests/unit/test_stream_partial_writes.py` and pipe regression tests:
  scripted send Futures and exact byte/callback transcripts.
- Stream/pipe e2e tests: small send buffers, slow peer, large writes,
  half-close and drain behavior.

1. Track the active immutable payload and offset separately from newly queued
   writes. On completion validate `0 < sent <= remaining`, then resubmit the
   unsent suffix before later writes. Zero progress on a non-empty payload
   receives a deterministic error path instead of a tight loop.
2. Keep unsent-byte and queued-byte accounting coherent across completions,
   submission errors and cancellation. Preserve high/low watermarks, pause/
   resume callbacks, empty-write behavior and pending drain waiter semantics.
3. Delay close/SHUT_WR until the entire active payload and later queued bytes
   drain. Abort discards only logically pending bytes while original native
   request ownership survives until the operation safely completes.
4. Preserve socket, pipe-HUP poll, SSL wrapper and connection-lost lifecycle
   behavior. Avoid changing native/CFFI send semantics to obscure the mismatch.

## Required tests and exit

Script results such as `[2, 1, remainder]` while additional writes arrive;
assert exact concatenated bytes, suffix buffers and buffer-size arithmetic.
Test completion error, cancel, zero/oversized result, synchronous submission
failure, empty write, pause/resume reentrancy, write EOF and close during an
active short send. Test socket and pipe subclasses independently.

Real TCP/Unix and pipe tests force small buffers and slow consumers, asserting
payload hashes/lengths rather than relying on timing. Existing subprocess and
TLS smoke checks preserve their wrappers. Record EPERM or other unavailable
kernel checks as outstanding rather than assuming fake completions prove
real-kernel behavior.

Lightweight review verifies no loss/reordering, valid numeric-result handling,
watermark arithmetic, exactly-once close, and all proactor/backend callers.
After high/medium fixes and re-review, persist the safety commit and continue
to native transport experiments. Benchmark baselines are rerun after this fix
because corrected work performed may change measured throughput.
