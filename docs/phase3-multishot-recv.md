# Phase 3 multishot receive and provided buffers

Branch: `feature/phase3-multishot-recv`

Depends on: `feature/phase3-stream-flow-control`.

## Deliverable and files

Connect the existing `Ring.prepare_recv_multishot()` and provided-buffer pool
to native stream transports. Touch `src/uringcore_liburing.c`,
`uringloop/native_proactor.py`, the private stub, direct native tests and stream
contracts. The existing pool permits one bounded pool per ring; make sharing
across transports explicit rather than assuming one pool per connection.

## Sequence

1. Configure pool entries/size, bounded total bytes and CQ capacity before
   arming receives. Account for pool registration failure and first-operation
   unsupported errors; select one-shot fallback visibly.
2. Route every CQE including non-final events to its transport generation.
   Validate selected-buffer ID and result length before access. A CQE's
   `MORE` flag is authoritative: rearm after termination only if still open,
   unpaused and not cancellation-pending.
3. Ordinary Protocol receives immutable bytes copied from the selected slot.
   Once the copy is owned by a delivery record, return the slot exactly once.
   The selected-buffer wrapper remains owned across any allocation failure
   until it can safely be returned; never leak or silently drop stream data.
4. Preserve completion order across bytes copies and rearm. EOF follows all
   earlier data. Bound deliveries per tick and total staged receive bytes so
   CQ storms do not starve timers, signals or other connections.
5. On pause/exhaustion, cancel production and retain completed bytes within
   the stated cap; resume after safely settling the original request. A
   race producing more completions is handled by ownership accounting,
   backpressure and explicit terminal/error policy, never unbounded spills.
6. `-ENOBUFS` terminates/retries according to actual request flags; wait for
   returned pool capacity and avoid immediate busy loops. Separate unsupported
   errors from genuine connection failures.

## Required tests and safety

Fake CQE streams cover MORE cleared after success, EOF, ENOBUFS, final error,
cancel racing successful data, duplicate/stale generation, allocation failure,
pool return and no-available-buffer retry. Real-kernel tests use tiny pools,
many streams, large sends split across slots, pause/resume and slow callbacks.

Verify `buffers_available + buffers_in_use == buffer_pool_entries` whenever
the pool is active, no double return, no return while an exported view exists,
and no ring destruction with kernel/consumer-owned slots. Existing
`ProvidedBuffer` memoryview lifetime tests remain authoritative. ASAN/UBSAN
covers repeated cancellation, CQ pressure and pool/ring shutdown.

## Measurement and exit

Compare one-shot and multishot receive with identical pool/callback budgets.
Record SQEs/submissions per delivered byte, pool peak usage, ENOBUFS/rearms,
copied bytes, throughput, CPU and tail latency. Do not call Protocol bytes
delivery zero-copy; materializing bytes still allocates and copies.

Exit requires passing ownership/rearm/backpressure tests, a documented
fallback on hosts without multishot support, and lightweight review clear of
high/medium findings. A benefit on one workload justifies keeping the option;
default enablement awaits the controlled integration matrix.
