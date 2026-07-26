import errno

import pytest

import uringloop.proactor
from uringloop.proactor import _ProactorSubmit


def test_flush_retries_partial_submissions(monkeypatch):
    submitter = _ProactorSubmit(object(), {})
    submitter._pending_submit = True
    submissions = iter((1, 1))
    ready = iter((1, 0))
    submit_calls = 0

    def submit(ring):
        nonlocal submit_calls
        submit_calls += 1
        return next(submissions)

    monkeypatch.setattr(uringloop.proactor, "io_uring_submit", submit)
    monkeypatch.setattr(uringloop.proactor, "io_uring_sq_ready", lambda ring: next(ready))

    submitter.flush()

    assert submit_calls == 2
    assert not submitter._pending_submit


def test_flush_preserves_pending_state_on_error(monkeypatch):
    submitter = _ProactorSubmit(object(), {})
    submitter._pending_submit = True

    def submit(ring):
        raise OSError(errno.EAGAIN, "try again")

    monkeypatch.setattr(uringloop.proactor, "io_uring_submit", submit)

    with pytest.raises(OSError, match="try again"):
        submitter.flush()

    assert submitter._pending_submit


def test_linked_submission_reserves_all_slots(monkeypatch):
    submitter = _ProactorSubmit(object(), {})
    submitter._pending_submit = True
    available = iter((1, 2))
    flushed = False

    def flush():
        nonlocal flushed
        flushed = True

    monkeypatch.setattr(uringloop.proactor, "io_uring_sq_space_left", lambda ring: next(available))
    monkeypatch.setattr(submitter, "flush", flush)

    submitter.ensure_capacity(2)

    assert flushed
