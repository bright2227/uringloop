#!/usr/bin/env python3
"""Run and summarize the pinned CPython-derived asyncio compatibility suite."""

import argparse
import ast
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from urllib.request import urlopen
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_MANIFEST = ROOT / "tests" / "compat" / "upstream.json"


def load_upstream(version: str) -> tuple[dict, dict]:
    manifest = json.loads(UPSTREAM_MANIFEST.read_text())
    try:
        return manifest, manifest["versions"][version]
    except KeyError as error:
        raise SystemExit(f"no tracked CPython asyncio revision for Python {version}") from error


def adaptation_methods_for_source(filename: str, source: str, adaptations: dict[str, dict[str, str]]) -> list[str]:
    tree = ast.parse(source)
    methods = []
    for local_test, adaptation in adaptations.items():
        reference = adaptation["upstream"]
        try:
            source_file, method = reference.split("::", 1)
        except ValueError as error:
            raise RuntimeError(f"invalid upstream adaptation reference for {local_test}: {reference}") from error
        if source_file != filename:
            continue
        owners = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method for child in node.body)
        ]
        if not owners:
            raise RuntimeError(f"upstream method {reference} for {local_test} was not found")
        if len(owners) != 1:
            raise RuntimeError(f"upstream method {reference} for {local_test} has ambiguous owners: {owners}")
        methods.append(f"{owners[0]}::{method}")
    return sorted(methods)


def local_contract_names(path: Path = ROOT / "tests" / "compat" / "test_cpython_asyncio.py") -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    }


def verify_adaptation_coverage(adaptations: dict[str, dict[str, str]]) -> None:
    local_tests = local_contract_names()
    manifest_tests = set(adaptations)
    missing = sorted(local_tests - manifest_tests)
    stale = sorted(manifest_tests - local_tests)
    if missing or stale:
        raise RuntimeError(f"adaptation coverage mismatch; missing={missing}, stale={stale}")
    missing_summaries = sorted(name for name, adaptation in adaptations.items() if not adaptation.get("summary", "").strip())
    if missing_summaries:
        raise RuntimeError(f"adaptations require non-empty summaries: {', '.join(missing_summaries)}")


def verify_upstream_sources(upstream: dict, adaptations: dict[str, dict[str, str]]) -> list[dict[str, object]]:
    verify_adaptation_coverage(adaptations)
    referenced_files = {adaptation["upstream"].split("::", 1)[0] for adaptation in adaptations.values()}
    missing_sources = sorted(referenced_files - upstream["sources"].keys())
    if missing_sources:
        raise RuntimeError(f"adaptations reference unverified upstream sources: {', '.join(missing_sources)}")
    verified = []
    for filename, expected in upstream["sources"].items():
        url = f"https://raw.githubusercontent.com/python/cpython/{upstream['commit']}/Lib/test/test_asyncio/{filename}"
        with urlopen(url, timeout=30) as response:
            contents = response.read()
        digest = hashlib.sha256(contents).hexdigest()
        if digest != expected:
            raise RuntimeError(f"upstream digest mismatch for {filename}: expected {expected}, found {digest}")
        methods = adaptation_methods_for_source(filename, contents.decode(), adaptations)
        verified.append({"file": filename, "methods": methods, "sha256": digest, "url": url})
    return verified


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
    parser.add_argument("--verify-upstream", action="store_true")
    args = parser.parse_args()
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    manifest, upstream = load_upstream(version)
    verified_sources = verify_upstream_sources(upstream, manifest["adaptations"]) if args.verify_upstream else []

    with tempfile.TemporaryDirectory() as directory:
        xml_path = Path(directory) / "compat.xml"
        process = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/compat", "-q", f"--junitxml={xml_path}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        if process.stdout:
            print(process.stdout, end="", file=sys.stderr)
        if process.stderr:
            print(process.stderr, end="", file=sys.stderr)
        summary = summarize(xml_path)

    document = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpython_asyncio_revision": upstream["tag"],
        "cpython_commit": upstream["commit"],
        "adaptations": manifest["adaptations"],
        "upstream_sources_verified": args.verify_upstream,
        "verified_sources": verified_sources,
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
