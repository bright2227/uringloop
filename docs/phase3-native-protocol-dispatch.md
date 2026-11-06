# Phase 3 native protocol-dispatch primitive

Branch: `feature/phase3-native-protocol-dispatch`

Depends on: `feature/phase3-stream-write-correctness`.

## Scoped deliverable

An experimental `ProtocolDispatcher` native type that owns cached bound
protocol callables and invokes them through vectorcall. This helper owns no
socket, transport state, request, callback Context, debug timing or loop
exception-handler policy. The caller supplies those lifecycle rules. It is
preparation for native transports and does not complete the stream-port branch
or establish a performance win.

## Files and implementation

- `src/uringcore_protocol.h`: included C helper, native type, callback cache,
  transaction/reentrancy guard and GC traversal/clearing.
- `src/uringcore_liburing.c`: type registration/export and internal ABI bump.
- `uringloop/_uringcore.pyi`: matching private signatures.
- `tests/unit/test_native_protocol_dispatch.py`: no-ring dispatch, failure,
  refcount and GC tests.
- `MANIFEST.in`: include the helper header so source distributions build.

Cache callable `connection_made`, `data_received`, `eof_received`,
`connection_lost`, `pause_writing` and `resume_writing`. Publish a replacement
only after all lookups/callability checks succeed. Lookup exceptions leave the
old complete cache unchanged and preserve their exception identity.

`get_protocol` returns the cached object; `set_protocol` refreshes the whole
cache. Explicit repeated `__init__` has the same atomic replacement semantics.
Attribute lookup can execute arbitrary Python: reject recursive replacement
during the lookup transaction, while permitting replacement from an invoked
callback. Hold an independent strong callable reference through vectorcall so
the callback can replace/decref the old cache safely. Release old cache only
after the new cache is complete, because decref can run finalizers.

The initial helper is ordinary Protocol-only. Reject a structural
`get_buffer` interface explicitly: BufferedProtocol's one-shot exporter path
is a separate planned feature, not an accidental `data_received` dispatch.
Cache changes to individual protocol methods take effect only after explicit
refresh/replacement. All callback results and exceptions pass through to the
caller; no exception is silently swallowed.

## Tests and exit

Test every callback's argument/result identity, exceptions, cache refresh,
noncallable/missing/throwing attributes, failed replacement, repeated init,
reentrant lookup/replacement, callback replacement while the old receiver
continues running, finalizer observations, uninitialized `__new__`, strong
reference release and cyclic GC involving cached methods/protocols.

Python 3.12/3.13 and ASAN/UBSAN tests require no io_uring setup. Source/wheel
checks verify the header and ABI agreement. Lightweight review focuses on
strong-reference lifetime, GC clearing, arbitrary attribute/finalizer
reentrancy and honest scope. Finish without a synthetic speedup claim; native
stream state/operation routing remains the next independent branch.
