#!/usr/bin/env python3
"""Run and summarize the pinned CPython-derived asyncio compatibility suite."""

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_REVISIONS = {
    "3.12": "v3.12.3",
    "3.13": "v3.13.0",
}


def summarize(xml_path: Path) -> dict:
    root = ET.parse(xml_path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {
        "tests": sum(int(suite.attrib.get("tests", 0)) for suite in suites),
        "failures": sum(int(suite.attrib.get("failures", 0)) for suite in suites),
        "errors": sum(int(suite.attrib.get("errors", 0)) for suite in suites),
        "skipped": sum(int(suite.attrib.get("skipped", 0)) for suite in suites),
        "seconds": sum(float(suite.attrib.get("time", 0)) for suite in suites),
    }
    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    skips = []
    for case in root.iter("testcase"):
        skipped = case.find("skipped")
        if skipped is not None:
            skips.append(
                {
                    "test": f"{case.attrib.get('classname', '')}.{case.attrib.get('name', '')}",
                    "reason": skipped.attrib.get("message") or (skipped.text or "").strip(),
                }
            )
    return {
        **totals,
        "passed": passed,
        "pass_rate_percent": 100 * passed / totals["tests"] if totals["tests"] else 0,
        "skip_reasons": skips,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if version not in UPSTREAM_REVISIONS:
        raise SystemExit(f"no tracked CPython asyncio revision for Python {version}")

    with tempfile.TemporaryDirectory() as directory:
        xml_path = Path(directory) / "compat.xml"
        process = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/compat", "-q", f"--junitxml={xml_path}"],
            cwd=ROOT,
            text=True,
        )
        summary = summarize(xml_path)

    document = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpython_asyncio_revision": UPSTREAM_REVISIONS[version],
        "summary": summary,
    }
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized)
    else:
        print(serialized, end="")
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
