# Phase 2 recovery provenance

Recovered on 2026-10-02 from the original Codex session and its reviewer logs.
The original completed checkout under `/tmp` and its Git objects were lost.
This independent repository starts at the preserved `215059a` contracts branch
and replays the successful recorded source patches in the original branch order.
Recovered commit hashes differ from the historical hashes cited in older docs.

The native queue, timer heap, eventfd, bounded tick, handle design prototypes,
integration selector, tests, and documentation were recovered through historical
reviewed tip `bfc6fa2`. This restores implementation; it does not constitute a
new review or a rerun of historical validation.

## Historical benchmark evidence

The contracts JSON was preserved in the original repository. Complete historical
ready-queue, eventfd, native-tick, and integration JSON documents were extracted
from reviewer tool outputs. Their SHA-256 values match the original integration
index exactly. Harness commit IDs and original timestamps were preserved.

Two source artifacts could not be recovered as complete documents:

- `benchmarks/results/phase2-timer-heap-2026-08-10-wsl2.json`
- `benchmarks/results/phase2-native-handles-2026-08-10-wsl2.json`

The logs retain their published summaries and the original integration index
retains their hashes. Their raw per-run samples were not printed in the saved
logs. Missing samples must not be recreated from summaries or represented as
historical measurements. Reproduce the design gate with newly dated evidence
on a suitable runner before using it for a new performance decision.

Recovery scripts, source manifests, and artifact log locations are stored in
the adjacent `phase3-recovery` directory. A Git bundle is stored there as a
durable backup. No branch was merged or pushed during recovery.

## Publication base — 2026-11-06

Remote `main` at `c18dc43` does not contain the later Phase 1 request core or
the recovered Phase 2 implementation. The Phase 2 publication branch includes
those Phase 1 prerequisites and merges that remote base, preserving its
cryptography constraint and dependency update. The recovery and historical
validation notes above describe the original recovery, not remote merge status.

Phase 2 remains opt-in through `loop_core="phase2"`; its controlled integrated
performance gate and the two missing raw artifacts remain open. Phase 3 is
published separately as an experimental dependent change.
