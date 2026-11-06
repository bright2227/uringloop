"""Regression gates for Python callbacks during native request retirement."""

import subprocess
import sys
import tempfile
import textwrap

import pytest

from uringloop import _uringcore


@pytest.mark.parametrize("owner", ["descriptor", "buffer"])
@pytest.mark.parametrize("action", ["close", "reinitialize", "reap_then_close"])
@pytest.mark.parametrize("batch_size", [1, 64])
def test_request_cleanup_can_change_ring_without_invalidating_reap(owner, action, batch_size):
    # A regression must produce a failing test with the child traceback rather
    # than taking down pytest itself. Sanitizer settings propagate to the child.
    script = textwrap.dedent("""
        import resource
        import sys
        import tempfile
        from uringloop import _uringcore

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        owner, action, batch_size = sys.argv[1], sys.argv[2], int(sys.argv[3])
        ring = _uringcore.Ring(8)
        payload = bytearray(1)
        events, errors, fresh = [], [], []
        sys.unraisablehook = lambda error: errors.append(repr(error.exc_value))

        def cleanup():
            events.append(action)
            if action == 'reinitialize':
                ring.__init__(8)
                fresh.append(ring.prepare_nop())
                ring.submit()
            else:
                if action == 'reap_then_close':
                    assert ring.reap() == []
                ring.close()

        with tempfile.TemporaryFile() as file:
            file.write(b'x')
            file.flush()

            class Descriptor:
                def fileno(self):
                    return file.fileno()

                def __del__(self):
                    cleanup()

            class Buffer:
                def __buffer__(self, flags):
                    return memoryview(payload)

                def __release_buffer__(self, view):
                    cleanup()

            if owner == 'descriptor':
                request = ring.prepare_read(Descriptor(), payload, 0)
            else:
                request = ring.prepare_read(file, Buffer(), 0)
            other = ring.prepare_timeout(60_000_000_000)
            ring.submit()
            [completion] = ring.wait(timeout_ns=1_000_000_000, max_completions=batch_size)
            assert completion.request is request
            assert completion.result == 1
            assert request.done and not request.cancelled
            assert other.done and other.cancelled
            assert payload == b'x'
            payload.extend(b'!')  # Every exporter must have been released once.
            assert events == [action]
            assert not errors, errors
            if action == 'reinitialize':
                assert ring.pending == 1
                [new_completion] = ring.wait(timeout_ns=1_000_000_000)
                assert new_completion.request is fresh[0]
                assert new_completion.result == 0
            else:
                assert ring.closed
            ring.close()
    """)
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script, owner, action, str(batch_size)],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("action", ["close", "reinitialize"])
def test_reap_argument_callback_cannot_invalidate_cqe_storage(action):
    with _uringcore.Ring(8) as ring:
        request = ring.prepare_nop()
        ring.submit()

        class Limit:
            def __index__(self):
                with pytest.raises(RuntimeError, match="busy"):
                    if action == "close":
                        ring.close()
                    else:
                        ring.__init__(8)
                return 1

        completions = ring.reap(Limit())
        if not completions:
            completions = ring.wait(timeout_ns=1_000_000_000)
        assert completions[0].request is request


def test_ring_teardown_prevents_reinitialization_from_nested_finalizer():
    errors = []
    ring = _uringcore.Ring(8)
    with tempfile.TemporaryFile() as file:
        class Descriptor:
            def fileno(self):
                return file.fileno()

            def __del__(self):
                ring.close()  # Reentrant close remains idempotent.
                try:
                    ring.__init__(8)
                except RuntimeError as error:
                    errors.append(str(error))

        request = ring.prepare_read(Descriptor(), bytearray(1), 0)
        ring.close()
        assert ring.closed and ring.pending == 0 and request.done
        assert len(errors) == 1 and "busy" in errors[0]


def test_abandoned_cancel_restores_target_for_later_cancellation():
    with _uringcore.Ring(8) as ring:
        target = ring.prepare_timeout(60_000_000_000)
        ring.submit()
        abandoned = ring.prepare_cancel(target)
        ring.abandon_prepared(abandoned)
        assert abandoned.kind == "nop"
        assert target.state == "submitted"
        ring.submit()
        [completion] = ring.wait(timeout_ns=1_000_000_000)
        assert completion.request is abandoned and completion.result == 0
        cancellation = ring.prepare_cancel(target)
        ring.submit()
        completions = []
        for _ in range(3):
            if len(completions) == 2:
                break
            completions.extend(ring.wait(timeout_ns=1_000_000_000))
        assert {item.request for item in completions} == {target, cancellation}
        assert target.cancelled and target.done
