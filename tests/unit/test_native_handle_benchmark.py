import contextvars
import gc
import json
from pathlib import Path
import weakref

import pytest

from benchmarks.native_handles import BACKENDS, HandleWorkload, worker_result


RESULT = Path(__file__).parents[2] / "benchmarks/results/phase2-native-handles-2026-08-10-wsl2.json"


def test_stock_handle_design_gate_worker_is_self_checking():
    result = worker_result("stock-python", HandleWorkload("handle", 32), warmup=0, repeats=1)

    assert result["workload"] == {"kind": "handle", "operations": 32}
    assert result["summary"]["allocation_operations_per_second_median"] > 0
    assert result["summary"]["dispatch_operations_per_second_median"] > 0
    assert result["summary"]["allocated_blocks_median"] > 0
    assert result["summary"]["traced_peak_bytes_median"] > 0


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("kind", ["handle", "timer"])
def test_handle_design_gate_choices_preserve_dispatch_and_cancellation(backend, kind):
    if backend != "stock-python":
        pytest.importorskip("benchmarks._native_handle_prototypes")

    result = worker_result(backend, HandleWorkload(kind, 32), warmup=0, repeats=1)

    assert result["summary"]["retained_shallow_bytes_median"] > 0
    assert result["summary"]["cancel_seconds_median"] >= 0


def test_full_native_prototype_owns_references_and_collects_cycles():
    prototypes = pytest.importorskip("benchmarks._native_handle_prototypes")

    class Loop:
        def get_debug(self):
            return False

        def call_exception_handler(self, context):
            raise AssertionError(context)

    loop = Loop()
    context = contextvars.Context()

    class Callback:
        def __init__(self):
            self.handle = None

        def __call__(self):
            return self.handle

    callback = Callback()
    handle = prototypes.NativeHandle(callback, (), loop, context)
    assert handle.get_context() is context
    assert handle._callback is callback
    handle.cancel()
    handle.cancel()
    assert handle.cancelled() is True
    assert handle._callback is None

    callback = Callback()
    cyclic_handle = prototypes.NativeHandle(callback, (), loop, context)
    callback.handle = cyclic_handle
    handle_reference = weakref.ref(cyclic_handle)
    del cyclic_handle, callback, handle
    gc.collect()
    assert handle_reference() is None


def test_full_native_prototype_pins_callback_arguments_across_reentrant_cancel():
    prototypes = pytest.importorskip("benchmarks._native_handle_prototypes")
    called = []

    class Context:
        handle = None

        def run(self, callback, *args):
            self.handle.cancel()
            return callback(*args)

    context = Context()
    handle = prototypes.NativeHandle(called.append, ("called",), object(), context)
    context.handle = handle

    handle._run()

    assert called == ["called"]
    assert handle.cancelled() is True


def test_captured_handle_design_gate_is_complete_and_tied_to_harness():
    document = json.loads(RESULT.read_text())

    assert document["schema_version"] == 1
    assert document["environment"]["git_commit"] == "108c8cb82a1f71022c17ff74040a3874762a6656"
    assert len(document["results"]) == 6
    assert {(result["backend"], result["workload"]["kind"]) for result in document["results"]} == {
        (backend, kind) for backend in BACKENDS for kind in ("handle", "timer")
    }
    for result in document["results"]:
        assert result["workload"]["operations"] == 20_000
        assert result["warmup_runs"] == 1
        assert result["measured_runs"] == 5
        assert len(result["samples"]) == 5
