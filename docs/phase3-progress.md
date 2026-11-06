# Phase 3 progress — 2026-10-02

Phase 3 is in experimental foundation work, not complete. The original recovery
and validation checkpoint below is dated 2026-10-02.

Publication update, 2026-11-06: remote `main` was verified at `c18dc43` and does
not contain the recovered Phase 2 stack. Phase 2 is published separately on
`feature/phase2-native-loop`, including its missing Phase 1 prerequisites and
the current remote dependency security update. `feature/phase3-native-transports`
replays the seven Phase 3 commits on that publication base for a dependent
draft PR. Neither phase is represented as merged or performance approved.
The original recovery checkout and unrelated Unix-test newline change remain
preserved. Historical checkpoint hashes below refer to that recovery history.

## Initialization-failure review follow-up — 2026-11-07

Native transport construction now separates an attempted initialization from
a successfully established state. Failed constructors leave methods guarded,
prevent repeated initialization, and preserve idempotent shutdown for objects
already exposed during registration. Successful closed transports retain
their metadata. Nine regressions cover early and late constructor failures,
an isolated child-process crash gate, and normal metadata after close.

The full local Python 3.12 suite passes: **653 passed, 7 skipped**. CI's
deterministic sanitizer step explicitly includes the new initialization tests.

## Request-lifetime review follow-up — 2026-11-06

The two final-review fixes from the Phase 2 base are inherited here: native
completion draining tolerates cleanup that closes or replaces the ring, and
failed Future/registration allocation cannot leave an unintended fd operation
queued. Phase 3 uses the shared unpublished-request rollback helper, including
its cancellation-target restoration and completion-model reset.

All 32 new kernel regression cases pass within the full suites. Local results
are **644 passed, 7 skipped** on Python 3.12 and **643 passed, 8 skipped** on
Python 3.13. The ASAN/UBSAN transport, kernel and event-loop gates pass with
**335 passed, 1 skipped**, with leak detection disabled.

## CI follow-up — 2026-11-06

The failures at the initial checkpoint below are fixed. Python socket
transport cleanup closes disconnected Linux sockets without the Windows
shutdown workaround and always detaches the server, including when the
protocol's connection-lost callback raises or reenters cleanup. The existing
eight-case TCP/Unix/native/Python/debug matrix is now included in a passing
full Python 3.12 run: **612 passed, 7 skipped**. Ruff passes.

The Phase 2 base now supports signal handlers and uses new, dated component
benchmark evidence. Its historical missing files remain documented. Sanitizer
builds explicitly supply setuptools. Performance approval and the remaining
Phase 3 features are still open; passing CI does not establish uvloop parity.

## Initial publication validation — 2026-11-06

Both extensions build on CPython 3.12.3; Ruff and diff checks pass. The ten
focused test files listed in `phase3-validation.md` pass: **234 tests**.

The eight-case stream e2e matrix was run outside the managed sandbox with real
io_uring and local socket access. Result: **6 passed, 2 failed**. All four
native TCP/Unix cases and both Python Unix cases pass. Both Python TCP cases
(debug off/on) time out in `server.wait_closed()` after the Python transport's
connection-lost callback raises `OSError: [Errno 107] Transport endpoint is not
connected` during shutdown. These failures remain open; the full Phase 3 suite,
sanitizers, Python 3.13, and performance matrix were not rerun for publication.

The Phase 2 base also has a failing signal-handler test and two missing
historical raw benchmark files. See `phase2-publication-validation.md`.
The dependent Phase 3 PR remains a draft; this run does not establish the
complete one-shot validation gate or authorize multishot/SEND_ZC enablement.

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
| `feature/phase3-native-stream-transports` | `44d9bf6` | dispatch/evidence tip `9b91baf` |
| `feature/phase3-stream-flow-control` | reviewed hardening checkpoint on this branch | `44d9bf6` |

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
- Native one-shot streams: introduced at ABI14, C state owns lifecycle, cached protocol
  callbacks, context, ordered writes, partial offsets and bounded read storage.
  The native proactor routes requests without per-operation Futures. A public
  asyncio Transport wrapper preserves protocol/server identity and sendfile
  fallback compatibility. Mutable writes are snapshotted; pending cancellation
  requests retain buffers until final completion. There are 32 native-state
  and initially 10 routing regressions. Lightweight peer review fixes cover GC ownership,
  server detach, malformed completions and exactly-once cleanup.
- Native streams are opt-in and use ordinary Protocol only. BufferedProtocol
  and initial TLS use the Python path; native `start_tls` is not supported.
  Write watermarks provide soft backpressure, not a hard output-memory cap.
  Each stream retains one 64 KiB writable receive buffer and at most one
  additional immutable pending payload of up to 64 KiB (plus object overhead).
- Flow-control/lifecycle hardening: ABI15 checkpoint adds 54 adversarial
  regressions covering exact high/low/zero boundaries, immediate limit changes,
  three drain waiters with one cancelled, terminal actions inside every callback,
  submission/cancel/SHUT_WR failure, bounded paused EOF, reentrant completion
  getters, scheduler/exception-handler failure and six seeded Python-oracle
  comparisons. Previously failing cases exposed stranded write producers,
  nonterminal scheduling failures and stale result application; these are fixed.
  Routing coverage now has 35 cases, including allocation and shutdown failures.
- Registration allocation recovery allocates the GC-tracked record before
  preparing its operation. If cache/set insertion fails, the new private
  `Ring.abandon_prepared` converts its unpublished local fd SQE into NOP,
  retaining CQE/buffer ownership until retirement. It cannot abandon submitted
  operations. Real-kernel NOP retirement tests are added but remain unverified.
- Initial lightweight reviews of flow state and rollback found no new high/medium
  findings. Review rerun: 124 focused tests pass. This is implementation review,
  not kernel-validation or integration approval.
- A subsequent fresh review found a prepared-SQE fd-reuse hazard on shutdown.
  The fix cancels unpublished stream operations by converting them to NOP,
  without submitting them merely to cancel them. If submission has already
  published SQEs and then fails, the proactor closes the ring before any socket
  cleanup, settles public Futures, force-detaches native servers synchronously,
  and re-raises the original error. That failure is fatal; the ring is not retried.
  Submitted-request cancellation remains unchanged. Buffer/CQE ownership is
  retained for local NOPs until final retirement; released requests are no-ops.
  Regression coverage includes publication-before-error (ENOMEM/no-progress/
  interrupt), reads/writes/both, peer cleanup failure, and server close without
  draining callbacks. Re-review found no remaining blocker in this narrow fix.
- Multishot receive/accept/poll, separate direct BufferedProtocol receive and
  transport SEND_ZC remain pending. Do not enable them before the one-shot
  stream kernel gates pass.

## Verification so far

Native/CFFI extensions built on CPython 3.12.3 and 3.13.7. On each interpreter,
249 focused unit tests and 30 ring-independent native core/EventFD/guard tests pass
(279 checks per interpreter). The latter run explicitly deselects 57
ring-dependent tests; it is not a full e2e-suite pass. Ruff and diff checks
pass for the new runtime and test files.

`scripts/verify_wheel.py` verified the local extensions have no dynamic
`liburing.so` dependency. The ABI14 and final ABI15 wheels were built, extracted into
isolated temporary directory, imported from outside the checkout and checked
for the same dependencies. This is not a clean pip-installed wheel result
(pip is unavailable here). The source distribution contains both native
headers and stream modules. ABI15 import and `Ring.abandon_prepared` presence
were verified from the isolated final wheel. Full-project Ruff checks pass.
A separate ABI15 ASAN/UBSAN build with the updated Python routing passed 200 focused transport/dispatcher tests, with
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

Full signal/fd-reuse stress remains outstanding. The allocation rollback does
not establish that asynchronous signals cannot interrupt between the Python
prepare/registration instructions; do not advertise that broader guarantee.
The eight-case TCP/Unix × Python/native × debug on/off e2e matrix collects;
collection is not a successful execution. CI includes deterministic native
transport contracts under sanitizers and the real kernel gates separately.

## Next implementation phase

Validate the reviewed one-shot stream/lifecycle checkpoints on an io_uring-
capable Linux runner, then continue `feature/phase3-multishot-recv` from the
flow-control branch. Commands and required evidence are in
`phase3-validation.md`. Native implementation does not close the outstanding
kernel-validation gate. Do not change sandbox permissions or promote defaults
to work around that prerequisite. Current checkout stays on the hardening tip;
later branches are planned, not represented as implemented or reviewed.
