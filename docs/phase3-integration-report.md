# Phase 3 integration and performance report plan

Branch: `feature/phase3-integration-report`

Depends on: `feature/phase3-datagram-pipes`.

Status: planned; no Phase 3 result or release approval is asserted here.

## Deliverable

Integrate the experimentally selected transport path, preserve oracle
comparisons and publish an auditable decision per feature and per overall
gate. Verify the recovered Phase 2 base before attributing any result to it.

## Integration sequence and touch points

1. Propagate all predecessor review fixes and verify the final linear stack
   against recorded bases/tips. Add integration-only regressions before
   changing runtime behavior further.
2. Run the selector matrix: Python/CFFI oracle; Python transports/native Phase
   1 core; Python transports/recovered Phase 2; native one-shot transports;
   native multishot; and separately toggled SEND_ZC/accept/poll optimizations.
   Skip only impossible combinations with an explicit reason. Stock asyncio
   and pinned uvloop are benchmark/compatibility comparators.
3. Exercise Python 3.12/3.13, debug/non-debug, Protocol/streams/
   BufferedProtocol, TCP/Unix, UDP/pipes, TLS, signals, threads, file I/O,
   sendfile, pidfd subprocesses and mixed timer/network fairness.
4. Run full differential tests, ASAN/UBSAN, GC/fd/request lifetime stress,
   feature-injection tests, sdist/wheel contents, no-system-liburing
   verification and a clean-installed wheel import/execution test.
5. Update `.github/workflows/ci.yml`, benchmark workflow, README selector
   documentation and ROADMAP status only to the degree supported by results.
   Do not convert unavailable kernel checks into successful results.
6. Persist all reviewed branches and a Git bundle in a durable project path.
   Raw JSON in `benchmarks/results/` identifies exact code/harness revisions;
   summarize outcome in this document when actual results exist.

## Controlled measurement

Reuse Phase 0/1 warmup, process isolation and recorded environment methodology.
Use a pinned controlled machine for published claims. Record CPU, kernel,
Python, liburing/uvloop versions, affinity/governor where applicable, setup
flags, probes, actual selected features, CQ/pool budgets, message sizes,
connection count and repetitions. Report median throughput/CPU/RSS,
p50/p99 latency, syscalls, CV and confidence/uncertainty method.

The published matrix includes 1/64/1000 plaintext connections, multiple small
and large message sizes, TCP and Unix, Protocol and streams, and slow-peer
backpressure. Separate connection-churn accept results, SEND_ZC threshold
sweeps, BufferedProtocol allocation/copy results, readiness fairness and
UDP/pipe/file regressions from steady-state echo claims. Preserve raw samples
and regressions, not just selected wins.

## Decision table to fill with evidence

| Gate | Required evidence | Allowed outcome before evidence |
| --- | --- | --- |
| Phase 2 prerequisite | Recovered source/artifacts and verified contracts; unresolved controlled comparison disclosed | Recovery/performance gate pending |
| Correctness | Differential and compatibility baseline at or above recorded baseline; EOF/half-close/backpressure/reentrancy pass | Experimental |
| Native safety | Sanitizer/lifetime/CQ pressure/notification/fd race checks pass | Experimental |
| Distribution | Self-contained wheel/sdist and clean installation pass | Experimental |
| Performance | Plaintext echo parity with or improvement over uvloop beyond uncertainty for the published matrix | No release-performance claim |
| Feature promotion | Measured benefit and reliable fallback for each optimization | Explicit opt-in |

A correctness/ownership failure is fixed before promotion. A feature with no
repeatable benefit can remain disabled or be removed with a documented no-go
decision. No blanket default switch follows automatically from a fast isolated
dispatch microbenchmark.

## Final lightweight review and exit

Review predecessor records, integrated callback/resource lifetimes, defaults,
fallback visibility, metadata/arithmetic reproducibility, documented skips,
packaging and residual limitations. Fix all high/medium findings and re-review.

Phase 3 implementation is considered integrated only when the code is
persisted and the available checks are reported accurately. The roadmap's
Phase 3 release exit requires controlled plaintext echo parity and passing
correctness/safety gates; if infrastructure prevents these checks, the report
must state that the release gate remains open. Phase 4 is not declared started
solely because this report document exists.
