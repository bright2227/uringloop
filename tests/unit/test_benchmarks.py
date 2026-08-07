from benchmarks.phase1 import Workload, matrix, parse_strace_summary, percentile
from scripts.compat_baseline import summarize


def test_full_benchmark_matrix_covers_phase_zero_dimensions():
    workloads = matrix("full")

    assert len(workloads) == 20
    assert {item.connections for item in workloads if item.api != "file"} == {1, 64, 1000}
    assert {item.message_size for item in workloads} == {64, 4096, 262144}
    assert {item.api for item in workloads} == {"socket", "streams", "file"}
    assert Workload("socket", 1000, 262144, 5) in workloads


def test_benchmark_percentiles_are_reported_in_microseconds():
    assert percentile([1_000, 2_000, 3_000], 0.5) == 2.0
    assert percentile([1_000, 2_000, 3_000], 0.99) == 3.0


def test_benchmark_operation_counts_match_each_api():
    assert Workload("socket", 4, 64, 10).operations == 40
    assert Workload("streams", 4, 64, 10).operations == 40
    assert Workload("file", 1, 4096, 10).operations == 10


def test_strace_summary_extracts_per_syscall_counts(tmp_path):
    summary = tmp_path / "strace.txt"
    summary.write_text(
        "% time seconds usecs/call calls errors syscall\n"
        " 60.00 0.001 10 12 2 io_uring_enter\n"
        " 40.00 0.001 5 8 epoll_wait\n"
        "100.00 0.002 8 20 2 total\n"
    )

    parsed = parse_strace_summary(summary)

    assert parsed["calls"] == {"io_uring_enter": 12, "epoll_wait": 8}
    assert parsed["total_calls"] == 20


def test_compatibility_baseline_records_skip_reasons(tmp_path):
    report = tmp_path / "compat.xml"
    report.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="1" time="0.1">'
        '<testcase classname="compat" name="passing" />'
        '<testcase classname="compat" name="skipped"><skipped message="documented reason" /></testcase>'
        "</testsuite>"
    )

    result = summarize(report)

    assert result["passed"] == 1
    assert result["pass_rate_percent"] == 50
    assert result["skip_reasons"] == [{"test": "compat.skipped", "reason": "documented reason"}]
