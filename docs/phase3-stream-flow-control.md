# Phase 3 stream flow control and lifecycle

Branch: `feature/phase3-stream-flow-control`

Depends on: `feature/phase3-native-stream-transports`.

## Deliverable and files

Complete the native stream state machine before adding repeated receives.
Primary files are `src/uringcore_liburing.c`, the private stub, loop factory
hooks, `tests/unit/test_transport_contracts.py` and
`tests/e2e/loop/test_stream_transport_contracts.py`. Add focused adversarial
subprocess tests under `tests/e2e/native` for crashes or shutdown races.

## Sequence and invariants

1. Keep one ordered write producer per stream. Advance the offset on partial
   sends; zero-progress non-empty writes are handled deterministically and
   cannot spin. EINTR/EAGAIN, resets and submission errors preserve the unsent
   byte count until completion or terminal discard.
2. Match asyncio high/low watermark semantics: pause above high, resume at or
   below low, one notification per transition, and immediate reevaluation
   after limit changes. Count queued/in-flight unsent bytes; acknowledge sent
   bytes once. Pinning already-sent zero-copy buffers later will be reported
   separately from logical write-buffer size.
3. Define `close` as drain pending writes then finalize; `abort` drops queued
   writes and starts cancellation. `write_eof` rejects subsequent writes and
   performs `SHUT_WR` only after queued bytes drain. Peer read EOF consults
   `eof_received`; true keeps the write half open where the transport permits.
4. `pause_reading` suppresses protocol data delivery and stops future read
   production. Any completion already racing with pause is held safely and
   delivered on resume; do not discard stream bytes. Resume is idempotent and
   cannot duplicate an active read.
5. Capture a dispatch snapshot, commit completion ownership before callbacks,
   and defer rearm/submission until its end. Callback writes/closes/pause,
   protocol changes and loop stop must invalidate stale snapshot entries by
   generation rather than freed pointers. Stop preserves unprocessed work for
   the next run and final resource cleanup.
6. Deliver `connection_lost` once after the logical close contract and keep
   native storage alive separately until all kernel-owned requests are safe.
   Loop shutdown settles cancelled requests before freeing ring storage.

## Required tests

Use scripted short-write/EOF/error completions for exact ordering. Exercise
tiny socket buffers with a slow peer, drain cancellation, zero/high/low limits,
watermark callback reentrancy, final bytes before EOF, independent half-closes,
close in connection-made/data/pause/resume/EOF callbacks, close while stopped,
protocol replacement, repeated abort and repeated loop lifecycle. Tests
assert no byte loss/duplication and bounded retained read data. Exception
handlers that themselves close or raise must not corrupt native state.

ASAN/UBSAN and GC/fd stress cover races with pending reads, writes and cancel
CQEs. Python 3.12/3.13 debug and non-debug behavior runs differentially.

## Measurement and exit

Measure small writes, batched writes and slow-reader RSS/latency. Record
logical write bytes, retained allocation bytes and pause/resume counts
separately; a bounded receive system does not promise that callers writing
without respecting backpressure have bounded output queues.

Lightweight review focuses on partial-send arithmetic, exactly-once teardown,
half-close order, reentrant callbacks and memory retention. Exit requires
locked contracts passing and explicit kernel/sanitizer evidence; this branch
is the prerequisite for multishot receive and zero-copy send.
