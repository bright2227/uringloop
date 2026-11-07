import pytest

import uringloop.loop
from uringloop.loop import IouringProactorEventLoop
import uringloop.proactor
from uringloop.proactor import IoUringProactor


def test_proactor_finalizer_handles_queue_initialization_failure(monkeypatch):
    def fail_queue_init(*args):
        raise OSError("io_uring unavailable")

    monkeypatch.setattr(uringloop.proactor, "io_uring_queue_init", fail_queue_init)
    proactor = IoUringProactor.__new__(IoUringProactor)

    with pytest.raises(OSError, match="io_uring unavailable"):
        proactor.__init__()

    proactor.__del__()


def test_event_loop_finalizer_handles_proactor_initialization_failure(monkeypatch):
    def fail_proactor_init():
        raise OSError("io_uring unavailable")

    monkeypatch.setattr(uringloop.loop, "IoUringProactor", fail_proactor_init)
    loop = IouringProactorEventLoop.__new__(IouringProactorEventLoop)

    with pytest.raises(OSError, match="io_uring unavailable"):
        loop.__init__()

    loop.__del__()
