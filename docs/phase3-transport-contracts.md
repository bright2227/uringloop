# Phase 3 transport contracts

Branch: `feature/phase3-transport-contracts`

Depends on: recovered and verified `feature/phase2-integration-report`.

## Deliverable and boundaries

Lock the existing Python transport behavior and the new experiment's design
before replacing hot paths. This branch can prepare tests and documentation
while Phase 2 is recovered, but its final base and results must identify the
verified recovered tip. Existing Phase 1/Python transport behavior remains
the oracle; historical performance conclusions are not new measurements.

## Touch points

- `tests/unit/test_phase3_transport_contracts.py` and
  `tests/unit/test_phase3_protocol_contracts.py`: deterministic fake-proactor
  transcripts for state and callback order, independent of kernel access.
- `tests/e2e/loop/test_stream_transport_contracts.py`: TCP and Unix stream
  observable behavior, explicitly parametrized for available implementations.
- `tests/compat/upstream.json` and `tests/compat/README.md`: attribution,
  pinned CPython source revisions and reviewed adaptations/skips when adding
  upstream-derived coverage.
- `benchmarks/phase3.py` and `tests/unit/test_benchmarks.py`: extend the Phase 1
  process-isolated harness with transport/Protocol and buffered-protocol modes.
- This plan and subsequent branch documents: record resource limits,
  selectors, callback snapshots and ownership transitions.

## Sequence

1. Inventory `_ProactorSocketTransport`, loop factories, `_start_serving`,
   sslproto hooks and transport methods on Python 3.12/3.13. Record method
   attributes expected by asyncio streams and sendfile without embedding
   private CPython object layouts in C.
2. Capture transcripts for `connection_made`, ordinary data, EOF,
   `connection_lost`, callback exceptions, pause/resume, `write_eof`, `close`
   versus `abort`, protocol replacement and server connection accounting.
3. Define context/debug behavior using the current loop's oracle. Test
   captured callback context, slow callback reporting and exception handler
   fields; avoid inventing a new synchronous completion ordering.
4. Write request/transport transition tables: NEW, OPEN, READ_PAUSED,
   WRITE_DRAINING, EOF_SENT, CLOSING, CLOSED. State combinations are explicit;
   write EOF and read EOF are independent flags.
5. Add benchmark schema for exact base/code/harness commits, selector,
   feature outcome, resource limits and unsupported runs. A requested mode
   must never be mislabeled after a fallback.

## Required cases

TCP IPv4/IPv6, Unix pathname/abstract addresses, supplied sockets,
connection-factory failure, connection-made write/close, zero-length writes,
mutable-buffer snapshot rules, partial send, write after EOF, peer half-close,
repeated close, unread final bytes, pause and immediate resume, replacement
protocol, GC cycles and fd ownership. Verify `get_extra_info`,
`get_write_buffer_size`, watermark validation and transport/server counts.
Tests use byte streams, not packet-boundary assumptions.

Callback transcripts include callbacks that write, abort, pause, change
protocol, call `loop.stop`, or raise. Split tests that need real io_uring from
deterministic model tests. Mark unavailable kernel tests as outstanding with
the exact error; do not expand the compatibility skip list to hide failures.

## Measurement and exit

Capture Python-transport baselines using the existing native proactor and
CFFI oracle plus stock asyncio and pinned uvloop. Include protocols/streams,
TCP/Unix, 1/64/1000 connections, small and large payloads, and slow-peer
backpressure. Schema/smoke validation can complete locally; controlled
comparisons remain a named later gate.

Lightweight review verifies contracts match supported CPython behavior,
the reentrancy rule is implementable, resource limits are enforceable, and
benchmark labels describe actual backends. Exit requires passing available
oracle/model tests and an explicit record of any real-kernel prerequisite.
