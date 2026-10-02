# Phase 3 progress — 2026-10-02

Phase 3 is in experimental foundation work, not complete. No branch has been
merged or pushed. The working stack is in the durable independent checkout
`/home/bright2227/uringloop/recovered-phase2`, because the original checkout's
Git metadata remains read-only. The original unrelated Unix-test newline
change is preserved and excluded from task commits.

## Recovered foundation

Phase 2's six feature checkpoints have been reconstructed from recorded
successful patches. The final recovered integration tip is `f174269`;
historical hashes are recorded separately in `RECOVERY.md` and the recovery
manifest. The corrected historical source blobs and recovered benchmark
artifacts match the logged originals. Two historical raw benchmark files
(timer heap and native handles) remain missing, not fabricated.

The recovered Phase 2 defaults remain unchanged: Phase 1 loop plumbing and
stock asyncio handles. Phase 2 is explicitly selectable, not performance
approved. User authorization permits Phase 3 experiments while the controlled
performance prerequisites remain open.

## Current work

Reviewed local branch checkpoints:

| Branch | Commit | Predecessor |
| --- | --- | --- |
| `feature/phase3-transport-contracts` | `03ef007` | Phase 2 `f174269` |
| `feature/phase3-stream-write-correctness` | `8702ea2` | `03ef007` |
| `feature/phase3-native-protocol-dispatch` | `276966b` | `8702ea2` |
| `feature/phase3-native-stream-transports` | implementation checkpoint on this branch | dispatch/evidence tip `9b91baf` |

- Transport baseline: 18 deterministic lifecycle, flow-control, EOF,
  BufferedProtocol, exception and callback-context tests. Quick review found
  and fixed a fake-proactor copy that could hide mutable-buffer bugs; the fake
  now retains the submitted memoryview. Re-review passed.
- Stream-write safety: counted-send socket factory and write-pipe mixin retry
  each unsent suffix before queued writes. Zero/invalid counts and cancelled
  writes abort rather than spin. There are 24 socket regressions and nine pipe
  regressions. Lightweight implementation and integration reviews passed.
  The public `sock_sendall` API has seven additional regressions for suffix
  retry and invalid/zero counts, preventing infinite zero-progress retries.
- Native dispatch prerequisite: `ProtocolDispatcher` owns GC-visible
  cached bound methods and invokes them through vectorcall. Atomic replacement,
  lookup failure, callback reentrancy, exception identity and cycle cleanup
  have 21 tests. Lightweight peer review passed. The helper alone is **not**
  an integrated transport; the following branch supplies ownership/routing.
- Benchmark scaffold: Protocol/BufferedProtocol × TCP/Unix × normal/slow-peer
  matrix with exact frame validation, actual transport classes, process
  isolation and unsupported/error results without synthetic timing samples.
  All review findings are fixed; 34 schema/framing/failure/selector regressions
  pass. `--transports native` independently selects the native stream path.
- Native one-shot streams: ABI14 C state owns lifecycle, cached protocol
  callbacks, context, ordered writes, partial offsets and bounded read storage.
  The native proactor routes requests without per-operation Futures. A public
  asyncio Transport wrapper preserves protocol/server identity and sendfile
  fallback compatibility. Mutable writes are snapshotted; pending cancellation
  requests retain buffers until final completion. There are 32 native-state
  and 10 routing regressions. Lightweight peer review fixes cover GC ownership,
  server detach, malformed completions and exactly-once cleanup.
- Native streams are opt-in and use ordinary Protocol only. BufferedProtocol
  and initial TLS use the Python path; native `start_tls` is not supported.
  Write watermarks provide soft backpressure, not a hard output-memory cap.
  Read retention is bounded to one 64 KiB one-shot buffer/pending payload.
- Flow-control/lifecycle hardening, multishot receive/accept/poll, separate
  direct BufferedProtocol receive and transport SEND_ZC remain pending.

## Verification so far

Native/CFFI extensions built on CPython 3.12.3 and 3.13.7. On each interpreter,
170 focused unit tests and 29 ring-independent native core/EventFD tests pass
(199 checks per interpreter). The latter run explicitly deselects 55
ring-dependent tests; it is not a full e2e-suite pass. Ruff and diff checks
pass for the new runtime and test files.

`scripts/verify_wheel.py` verified the local extensions have no dynamic
`liburing.so` dependency. The ABI14 wheel was also built, extracted into an
isolated temporary directory, imported from outside the checkout and checked
for the same dependencies. This is not a clean pip-installed wheel result
(pip is unavailable here). The source distribution contains both native
headers and stream modules.
A separate ABI14 ASAN/UBSAN build passed 121 focused transport/dispatcher tests, with
`ASAN_OPTIONS=detect_leaks=0`; this is not a kernel or leak-detector pass.

The first nine local benchmark smoke attempts produced no successful measurements:
four TCP bind errors, four Unix EPERM unavailable results, and one native
io_uring EPERM unavailable result. Every result has no samples and no summary.
Raw capture files retain their dirty-tree flag and harness hash; they are
failure evidence, not performance evidence.

An additional native-stream smoke matrix captured eight EPERM results in
`benchmarks/results/phase3-native-stream-smoke-2026-10-02.json`, explicitly
requesting `transport_mode="native"`. Every result has an empty sample list
and null summary. The harness exits nonzero for this unavailable matrix.

Final lightweight peer review found no new blockers in the native-stream
branch's e2e harness, defaults/fallbacks, benchmark selector or packaging.
The additional 76-test focused review rerun passed. Kernel-backed execution
and performance remain open; this review does not grant integration approval.

Creating `_uringcore.Ring()` fails with `PermissionError: [Errno 1] Operation
not permitted`. The new native TCP large-payload/half-close e2e test fails at
ring construction with EPERM, before any socket/transport assertion. It is not
skipped or counted as a pass. Real TCP/Unix/pipe io_uring tests, kernel cancellation races,
and the integrated performance matrix remain outstanding. No EPERM check is
reported as a successful network benchmark. The Phase 3 uvloop-parity and
release gates remain open.

## Next implementation phase

Harden the integrated native one-shot lifecycle/flow control before enabling
multishot receive. Use the detailed linear stack in `phase3-plan.md`; native
implementation does not close the outstanding kernel-validation gate.
Kernel-backed validation requires
a runner where io_uring is permitted; do not change sandbox permissions or
promote defaults to work around that prerequisite.
