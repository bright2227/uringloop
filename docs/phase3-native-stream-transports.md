# Phase 3 native TCP and Unix stream transports

Branch: `feature/phase3-native-stream-transports`

Depends on: `feature/phase3-native-protocol-dispatch`.

## Deliverable

An explicit opt-in native stream transport that initially uses ordinary
one-shot receive/send operations. Moving lifecycle and protocol dispatch
first isolates dispatch overhead from multishot and zero-copy effects.

The stream-write branch's Python `_PartialWriteMixin` and socket/pipe subclasses are
a safety correction, not native transport delivery. A separately tested C
ProtocolDispatcher helper can be prepared first, but only integrated native
state/operation routing satisfies this branch's native-transport outcome.

## Touch points

- `src/uringcore_liburing.c`: native transport state, GC-owned references,
  cached protocol methods, request-to-transport routing and dispatch. Factor
  helper source/header only if the established extension build supports it;
  shared ring internals have one integration owner.
- `uringloop/_uringcore.pyi`: private transport and completion interfaces;
  bump internal ABI coherently with runtime version checks/tests.
- `uringloop/native_proactor.py`: register transport-owned operations without
  allocating/resolving a Future for each receive; retain Future requests for
  public socket/file APIs and lifecycle coordination.
- `uringloop/loop.py`: `_make_socket_transport` and Unix/TCP connection/server
  factory selection, constructor validation and TLS routing.
- `setup.py`, `.github/workflows/ci.yml`: build changes only if needed,
  selected-path safety/differential checks.

## Sequence

1. Add native state with strong loop, socket, protocol and cached-method
   references, `tp_traverse`/`tp_clear`, safe partially constructed deallocation,
   and one terminal connection-lost notification.
2. Complete a factory transaction: validate selector/backend, install native
   transport, schedule connection-made in oracle order, resolve waiter only
   after successful setup, and unwind server/socket counts exactly once.
3. Submit one read and ordered writes using existing request-owned buffers.
   Map completions to native transport delivery records rather than Futures.
4. Dispatch cached bound `data_received` with vectorcall while the GIL is
   held. Refresh the complete cached method set on `set_protocol`; an
   allocation/attribute error leaves the old protocol transaction intact.
5. Retain a bounded completion snapshot and safe shutdown skeleton; the next
   branch fills all edge cases before advertising multishot support.
6. Keep SSL wrapped through sslproto and retain existing Python datagram/pipe
   factories until their own branch.

## Ownership and failures

The native record cannot free a request buffer while the kernel owns it.
Write input is snapshotted where asyncio permits mutable caller buffers;
pinning a buffer alone does not prevent caller mutation. The transport keeps
its socket alive until operation shutdown, and socket close/fd reuse cannot
make an old CQE target a new transport. Callback and exception-handler calls
can execute arbitrary Python: take strong temporary references and recheck
generation/state after each call. No Python references cross a GIL-free wait.

## Validation and measurement

Run the branch-one transcripts against both paths, real TCP/Unix streams,
supplied-socket cleanup, protocol factory failure, TLS smoke, debug/context
behavior and repeated GC/close. ASAN/UBSAN covers construction failure,
cyclic protocols, cancellation and short reads/writes.

Measure one-shot native dispatch versus Python transports on the same native
ring and identical payload/concurrency. Report callback count, CPU, throughput,
latency, allocations and RSS. This comparison does not claim multishot wins.

Exit requires no unresolved high/medium lightweight review finding in object
lifetime, callback ordering, selector validation, protocol replacement,
factory failure or stub/ABI agreement. Missing kernel results remain explicit
prerequisites for integration rather than inferred passes.
