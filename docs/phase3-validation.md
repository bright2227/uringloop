# Phase 3 validation handoff

Run from the dependent branch tip in `recovered-phase2`. No defaults should be
promoted, branches merged, or multishot/SEND_ZC enabled based only on scripted
completions. The current sandbox denies both io_uring and local networking.

## Deterministic coverage

Use an environment with the development dependencies and built extensions:

```bash
uv run python setup.py build_ext --inplace
uv run pytest -q \
  tests/unit/test_phase3_transport_contracts.py \
  tests/unit/test_phase3_protocol_contracts.py \
  tests/unit/test_stream_partial_writes.py \
  tests/unit/test_write_pipe_partial_writes.py \
  tests/unit/test_sock_sendall_counts.py \
  tests/unit/test_native_protocol_dispatch.py \
  tests/unit/test_native_stream_transport.py \
  tests/unit/test_native_stream_flow_control.py \
  tests/unit/test_native_transport_proactor.py \
  tests/unit/test_phase3_benchmarks.py
```

Run on Python 3.12 and 3.13. CI additionally instruments the native transport
contracts with ASAN/UBSAN; leak detection is disabled, not a passing leak audit.

## Required Linux runner gates

Use a runner that permits `io_uring_setup`, local TCP/Unix socket operations,
and the relevant cancellation syscalls. Do not hide EPERM using test skips.
Kernel feature absence must be reported as unavailable, with no timing samples.

```bash
uv run pytest -q tests/e2e/native/test_native_stream_e2e.py
uv run pytest -q tests/e2e/native tests/e2e/loop tests/e2e/proactor
uv run python -m benchmarks.phase3 --backends native \
  --transports python --profile smoke --output artifacts/phase3-python-smoke.json
uv run python -m benchmarks.phase3 --backends native \
  --transports native --profile smoke --output artifacts/phase3-native-smoke.json
```

The stream e2e matrix checks TCP/Unix, Python/native transports and debug on/off,
with large verified payloads, drain, EOF and close. Initial SSL/BufferedProtocol
still uses the Python path, and native `start_tls` remains unsupported. These
tests do not cover every kernel cancellation/fd-reuse race; add that stress
evidence before integrated approval. Confirm actual transport classes in each
benchmark result. Requested native mode is not proof that BufferedProtocol
used a native transport.

Capture failures and cleanup errors as failures, not performance results.
Run the full benchmark profile only after correctness gates pass. Preserve
kernel/Python/CPU metadata, source revision, harness hash and dirty-tree flag.
Phase 2's controlled approval and two missing historical raw files remain
separate unresolved prerequisites; see `RECOVERY.md`.
