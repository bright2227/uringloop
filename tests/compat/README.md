# CPython asyncio compatibility baseline

These seven contract tests adapt behavioral assertions from CPython's asyncio
tests without copying their helper framework. `upstream.json` maps every local
test to its upstream method and records SHA-256 digests for every referenced
source file. The tracked upstream revisions are:

- Python 3.12: tag `v3.12.3`, commit `f6650f9ad73359051f3e558c2431a109bc016664`
- Python 3.13: tag `v3.13.0`, commit `60403a5409ff2c3f3b07dd2ca91a7a3e096839c7`

The adapted behavior is attributed to the Python Software Foundation under
PSF License Version 2 in `THIRD_PARTY_LICENSES/CPython-PSF-2.0.txt`.

The source areas are `Lib/test/test_asyncio/test_events.py`,
`test_base_events.py`, `test_tasks.py`, `test_streams.py`, and
`test_subprocess.py`. Each local test runs the behavior through
`IoUringProactorEventLoop`, not the pytest-asyncio default loop.

Every skip must include a reason in the marker. `scripts/compat_baseline.py`
runs this directory, parses JUnit XML, and emits the test revision, pass rate,
failure counts, and every skip reason as JSON. CI uploads one baseline per
supported Python version.

CI passes `--verify-upstream`, downloads those exact primary-source files,
and refuses to publish a baseline if any digest differs. This is a deliberately
small compatibility contract, not a claim that uringloop runs CPython's full
asyncio test package unchanged. Expanding the adapted set remains Phase 2
correctness work.
