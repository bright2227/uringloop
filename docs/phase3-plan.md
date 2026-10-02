# Phase 3: native transports and protocol dispatch

Status: experimental development authorized on 2026-10-02; implementation
and release gates remain pending. This plan does not authorize a merge or a
default change.

## Starting point and evidence

The original checkout currently ends at `feature/phase2-loop-contracts`,
`215059a`. Its `docs/phase2-integration-report.md` is a plan, not evidence that
integration happened in that checkout. The later Phase 2 branch stack was
historically implemented and reviewed in a temporary clone; that clone is
gone. The recovery handoff now exists in the durable project checkout
`recovered-phase2/`, at reconstructed integration tip `f174269`, including
the final historical review-tail corrections. The ABI12 extension was built
locally and deterministic foundation checks pass; real-kernel verification
remains blocked by `io_uring_setup` returning EPERM. Reconstructed commit IDs differ from
historical IDs; preserve both mappings in recovery evidence.

The prior session reported these Phase 2 conclusions: an explicit
`loop_core="phase2"` experimental selector, Phase 1 retained as the default,
stock asyncio handles retained after native prototypes regressed overall, and
controlled scheduling/network measurements still outstanding. These are
historical session conclusions until recovered code and artifacts verify
them. They do not establish uvloop parity or satisfy the controlled Phase 2
performance gate. User authorization permits Phase 3 experiments while that
gate remains open; production-default decisions still require evidence.

The persistent Phase 1 implementation already has request-owned buffers,
multi-completion lifetime tracking, `prepare_recv_multishot`,
`prepare_send_zc`, a bounded provided-buffer ring, one-shot `prepare_poll`,
Python transport factories, and pidfd subprocess handling. These are reusable
mechanisms; Phase 3 must connect them to transport semantics rather than
replace their ownership contracts. Multishot accept and poll are additional
native request operations, not capabilities already exposed by this checkout.

## Linear branch stack

Every branch starts from its predecessor's reviewed tip. Recovery is tracked
separately; do not silently branch runtime implementation from the incomplete
persistent Phase 2 contracts tip.

| Order | Branch | Depends on | Delivery |
| ---: | --- | --- | --- |
| 1 | `feature/phase3-transport-contracts` | Recovered, verified Phase 2 integration tip | Differential behavior tests, lifecycle design, feature controls, benchmark schema. |
| 2 | `feature/phase3-stream-write-correctness` | transport contracts | Fix inherited transport handling of partial native/CFFI sends before optimizing streams. |
| 3 | `feature/phase3-native-protocol-dispatch` | stream write correctness | GC-aware cached vectorcall helper; preparatory primitive only. |
| 4 | `feature/phase3-native-stream-transports` | native protocol dispatch | Opt-in native TCP/Unix transport and direct dispatch with one-shot I/O. |
| 5 | `feature/phase3-stream-flow-control` | native stream transports | Backpressure, half-close, pause/resume, shutdown and callback reentrancy. |
| 6 | `feature/phase3-multishot-recv` | stream flow control | Bounded provided-buffer receive, cancellation/rearm and feature fallback. |
| 7 | `feature/phase3-buffered-protocol` | multishot recv | Separate one-shot receive into `BufferedProtocol.get_buffer()`. |
| 8 | `feature/phase3-send-zc` | buffered protocol | Opt-in size-gated zero-copy sends with notification-owned lifetime. |
| 9 | `feature/phase3-multishot-accept` | send zc | Native repeated accept, listener shutdown, accepted-fd lifetime. |
| 10 | `feature/phase3-readiness` | multishot accept | `add_reader`/`add_writer`, replacement/removal, one-shot and multishot poll. |
| 11 | `feature/phase3-datagram-pipes` | readiness | UDP and pipe transports; sendfile, sslproto and pidfd regressions. |
| 12 | `feature/phase3-integration-report` | datagram/pipes | Integrated experiment, safety/distribution gates and controlled performance decision. |

The corresponding `phase3-*.md` documents define each branch's files,
implementation sequence, meaningful tests, measurements, and review exit.
Earlier prototype branches may be prepared independently, but their commits
must be integrated onto this chain and reviewed after all prerequisite fixes.

## Current objective and progress checklist

Deliver the reviewed, persisted Phase 3 branch stack with explicit experimental
selectors, preserved oracle paths and accurate integration evidence. Finish
the stream-write safety correction before network hot-path optimization.

- [x] Detailed feature-branch plan written in the original workspace.
- [x] Phase 2 recovery handoff identified at reconstructed tip `f174269`.
- [x] Recovered Phase 2 build and available deterministic verification recorded
  in `RECOVERY.md`; missing raw evidence and kernel gates remain explicit.
- [x] Transport contracts baseline/scaffold implemented and reviewed;
  kernel-dependent coverage remains outstanding.
- [x] Stream-write correctness implemented and reviewed with deterministic
  socket, pipe and public API regressions; real I/O remains outstanding.
- [x] Native protocol-dispatch helper implemented and reviewed as a scoped
  prerequisite; a dispatch helper alone is not a native transport.
- [x] Native one-shot TCP/Unix implementation checkpoint reviewed; real-kernel
  verification remains outstanding and native mode stays opt-in.
- [x] Lifecycle/flow-control hardening implementation checkpoint reviewed; kernel gate remains
  mandatory before multishot and integration approval.
- [ ] Multishot receive and separate BufferedProtocol branch reviewed.
- [ ] SEND_ZC, multishot accept and readiness branches reviewed.
- [ ] UDP/pipe integration and preserved-path checks reviewed.
- [ ] Final report, durable branch bundle and available verification completed.
- [ ] Controlled Phase 2 and Phase 3 performance gates resolved separately.

Update checkboxes only from code and recorded verification evidence. A plan,
standalone helper or synthetic test pass cannot mark a full feature complete.

## Architecture decision

The initial native transport path is an explicit experiment on the native
proactor. Keep the CFFI/Python transports as the behavioral oracle. Specify
the final selector against the recovered loop signature before implementing
it; the intended spelling is `transports="native"` with the existing Python
path as its default. Selecting native transports with an incompatible backend
raises a precise error before taking socket ownership.

Transport state and request ownership live in native records/types. Reuse the
existing ring, request and selected-buffer lifetime machinery. Resolve
completions to transport state and invoke cached bound protocol methods with
vectorcall; normal data dispatch must avoid an intermediate Future per receive
and Python transport/proactor frame chain. A Python coordinator may choose
factories and route non-transport requests. Private APIs receive matching
stubs and ABI changes; do not advertise a stable transport ABI.

Callbacks run with the GIL. Kernel wait may release the GIL; no Python access
occurs in that region. Ring completions first commit ownership/state into a
bounded dispatch snapshot, then callbacks run. Mutations requested by those
callbacks update logical state immediately, but SQE submission and request
rearming happen after the current dispatch pass. Generation checks suppress
stale callbacks following close, replacement, or protocol change. The design
must allow ordinary callback operations (`write`, `close`, pause, stop,
`set_protocol`) without recursively draining completions. Strong references
protect every receiver and cached method for the duration of a callback.

`set_protocol` refreshes the cached method set as a transaction. Cache the
protocol's callable attributes at connection setup/replacement, consistent
with the locked compatibility contract; do not cache borrowed references.
Callback exceptions preserve asyncio's exception-handler context, debug
timing, and close behavior. A callback that stops the loop does not abandon
kernel-owned resources or lose the undelivered snapshot.

## Bounded resources and fallbacks

- Read/write request counts, receive pool bytes, held deliveries, pending
  accepts, and callback dispatch per tick have explicit limits and counters.
- Paused reads stop new receive work. Already completed bytes are retained in
  bounded storage; exhaustion cancels/rearms the producer instead of allocating
  an unbounded spill queue or dropping stream bytes.
- Linux 5.19 remains the support floor. Opcode probes, registration results,
  and first-operation results determine feature availability; an opcode probe
  alone does not establish support for every flag.
- Multishot receive falls back to one-shot receive. Zero-copy send falls back
  to ordinary send. Multishot accept/poll fall back to rearmed one-shot
  operations. Test each unsupported-feature transition explicitly.
- `BufferedProtocol` uses its own writable buffer with one-shot receive.
  Ordinary Protocol dispatch materializes immutable bytes from provided
  buffers and counts that copy honestly.
- TLS retains CPython `sslproto`; native SSL and free-threaded support are
  separate projects. Preserve pidfd subprocess handling and the existing
  sendfile implementation, including its backend-specific fallback.

## Parallel work and integration ownership

Subagents can prepare transport contract tests/fixtures, benchmark tooling,
and narrow native-operation prototypes in isolated persistent checkouts with
disjoint file ownership. Assign one integration owner for the shared
`src/uringcore_liburing.c`, `uringloop/_uringcore.pyi`, loop factories and ABI
number. Concurrent modifications to those files require serialized integration.
Each independent prototype records its base commit and tests; its review is
repeated against the final predecessor revision.

Persist all commits, patches, raw results and a Git bundle inside the
workspace or another durable project directory. Temporary build artifacts can
live in `/tmp`; the only copy of completed source/history cannot. Existing
unrelated worktree edits, including `tests/e2e/loop/test_unix.py`'s newline
change, are excluded from task commits.

## Per-branch completion and final gate

1. Lock observable behavior before implementation; add fake-completion tests
   for state races that are difficult to reproduce on a real kernel.
2. Run focused Python 3.12/3.13 differential tests, native safety stress and
   the relevant benchmark smoke. Kernel-denied checks are recorded separately
   and remain outstanding; synthetic passes do not substitute for real I/O.
3. Perform the requested lightweight review of the branch diff, fix all
   high/medium findings and repeat review until clear.
4. Record the reviewed commit, commands/results, raw benchmark provenance,
   feature status and limitations; preserve the branch before advancing.

The integration report decides separately whether correctness, safety,
packaging and performance pass. Phase 3's release performance bar is plaintext
echo parity with or improvement over pinned uvloop for the published workload
matrix, beyond measured uncertainty. A smoke result cannot approve that bar.
Every optimization remains selectable for comparison until its benefit and
fallback are verified; regressions and no-go outcomes are reportable results.
