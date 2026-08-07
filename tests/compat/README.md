# CPython asyncio compatibility baseline

These tests adapt behavioral assertions from CPython's asyncio tests without
copying their helper framework. The tracked upstream revisions are:

- Python 3.12: tag `v3.12.3`
- Python 3.13: tag `v3.13.0`

The source areas are `Lib/test/test_asyncio/test_events.py`,
`test_base_events.py`, `test_tasks.py`, `test_streams.py`, and
`test_subprocess.py`. Each local test runs the behavior through
`IoUringProactorEventLoop`, not the pytest-asyncio default loop.

Every skip must include a reason in the marker. `scripts/compat_baseline.py`
runs this directory, parses JUnit XML, and emits the test revision, pass rate,
failure counts, and every skip reason as JSON. CI uploads one baseline per
supported Python version.
