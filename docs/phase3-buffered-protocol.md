# Phase 3 BufferedProtocol receive

Branch: `feature/phase3-buffered-protocol`

Depends on: `feature/phase3-multishot-recv`.

## Deliverable and files

A separate receive mode for `asyncio.BufferedProtocol`: call `get_buffer`,
receive directly into that exported buffer using a one-shot request, then
call `buffer_updated`. Primary changes are native transport state/dispatch,
the private stub and protocol/fake-completion tests, plus real stream tests.
Do not reuse kernel-selected provided buffers for this API.

## Sequence and ownership

1. Choose Protocol versus BufferedProtocol mode using the pinned asyncio
   factory behavior, including protocol replacement. Cache required methods
   transactionally and retain the correct protocol generation per request.
2. Invoke `get_buffer(sizehint)` with the GIL; it may reenter, raise, return an
   invalid buffer or close the transport. Revalidate open/unpaused state before
   exporting and preparing receive. Reject empty, readonly or unsupported
   non-contiguous buffers with compatible error/close reporting.
3. Hold a writable `Py_buffer` and its exporter until the original operation
   completes; cancellation success alone cannot release kernel-owned memory.
   Do not call `get_buffer` again while that export is in use.
4. On positive completion, commit request state then call
   `buffer_updated(nbytes)` once with validated bounds. Zero indicates EOF;
   route to `eof_received` without a zero-byte buffer-updated callback unless
   the oracle contract requires otherwise.
5. After callback, release the completed export and rearm only if the
   transport generation is still valid. Replacement cannot deliver an old
   exporter completion to the new protocol. Paused final data is retained
   safely with bounded ownership and delivered according to the locked model.

## Required tests

Bytearray/memoryview exporters, different buffer sizes each call, sliced
contiguous buffers, zero-length/readonly/non-contiguous rejection, resize
while exported, custom exporters, get-buffer exceptions, buffer-updated
exceptions, close/abort/pause/replacement in either callback, cancellation
race, EOF and loop stop/restart. Compare callback transcripts with supported
CPython behavior. ASAN/UBSAN covers exporter lifetime and cyclic protocols.

## Measurement and exit

Benchmark ordinary Protocol bytes copy versus BufferedProtocol one-shot
direct receive. Count get-buffer calls, copies/allocations, CPU, throughput,
RSS and tail latency. The result isolates one-shot direct receive; it makes
no zero-copy multishot claim.

Exit requires safe exported-buffer ownership, differential contract pass,
unsupported-feature fallback unaffected, and no high/medium lightweight
review findings. Protocol replacement mode changes receive their own state
transition tests before integration.
