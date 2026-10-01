import pytest

from uringloop import _uringcore
from uringloop.lib import POLLIN
from uringloop.loop import IoUringProactorEventLoop


class FakeFuture:
    def __init__(self):
        self.callback = None
        self.was_cancelled = False

    def add_done_callback(self, callback):
        self.callback = callback

    def cancelled(self):
        return self.was_cancelled

    def cancel(self):
        self.was_cancelled = True

    def result(self):
        return POLLIN

    def finish(self):
        self.callback(self)


class FakeProactor:
    def __init__(self):
        self.polls = []

    def poll_add(self, file, mask):
        future = FakeFuture()
        self.polls.append((file, mask, future))
        return future


class ConstructorProactor:
    def __init__(self, *, fail_poll=False):
        self.loop = None
        self.fail_poll = fail_poll
        self.closed = False

    def set_loop(self, loop):
        self.loop = loop

    def poll_add(self, file, mask):
        if self.fail_poll:
            raise PermissionError("injected poll failure")
        return self.loop.create_future()

    def recv(self, file, size):
        return self.loop.create_future()

    def select(self, timeout=None):
        return []

    def close(self):
        self.closed = True


def eventfd_loop_state():
    loop = IoUringProactorEventLoop.__new__(IoUringProactorEventLoop)
    loop._closed = True
    loop._proactor = FakeProactor()
    loop._eventfd_wakeup = _uringcore.EventFD()
    loop._eventfd_poll_future = None
    loop._eventfd_closing = False
    loop._eventfd_poll_completions = 0
    return loop


def test_eventfd_poll_drain_and_rearm_state_machine():
    loop = eventfd_loop_state()
    try:
        loop._arm_eventfd_poll()
        first = loop._eventfd_poll_future
        assert loop._proactor.polls == [(loop._eventfd_wakeup, POLLIN, first)]

        assert loop._eventfd_wakeup.notify() is True
        first.finish()

        assert loop._eventfd_wakeup.signalled is False
        assert loop._eventfd_poll_future is not first
        assert len(loop._proactor.polls) == 2
    finally:
        loop._eventfd_wakeup.close()


def test_eventfd_cancel_race_rearms_unless_loop_is_closing():
    loop = eventfd_loop_state()
    try:
        loop._arm_eventfd_poll()
        first = loop._eventfd_poll_future
        first.cancel()
        first.finish()
        second = loop._eventfd_poll_future
        assert second is not first

        loop._eventfd_closing = True
        second.cancel()
        second.finish()
        assert loop._eventfd_poll_future is None
        assert len(loop._proactor.polls) == 2
    finally:
        loop._eventfd_wakeup.close()


def test_eventfd_poll_rejects_double_arm():
    loop = eventfd_loop_state()
    try:
        loop._arm_eventfd_poll()
        try:
            loop._arm_eventfd_poll()
        except RuntimeError as error:
            assert "already armed" in str(error)
        else:
            raise AssertionError("double eventfd poll arm was accepted")
    finally:
        loop._eventfd_wakeup.close()


def test_eventfd_loop_keeps_signal_socketpair_separate_from_thread_notification():
    proactor = ConstructorProactor()
    loop = IoUringProactorEventLoop(proactor, thread_wakeup="eventfd")
    try:
        assert loop._eventfd_wakeup.fileno() not in {loop._ssock.fileno(), loop._csock.fileno()}
        loop.call_soon_threadsafe(lambda: None)
        assert loop._eventfd_wakeup.signalled is True
        with pytest.raises(BlockingIOError):
            loop._ssock.recv(1)
    finally:
        loop.close()

    assert proactor.closed is True


def test_eventfd_poll_setup_failure_aborts_loop_and_closes_proactor():
    proactor = ConstructorProactor(fail_poll=True)

    with pytest.raises(PermissionError, match="injected poll failure"):
        IoUringProactorEventLoop(proactor, thread_wakeup="eventfd")

    assert proactor.closed is True


def test_eventfd_descriptor_setup_failure_aborts_loop_and_closes_proactor(monkeypatch):
    proactor = ConstructorProactor()

    def fail_eventfd():
        raise PermissionError("injected eventfd failure")

    monkeypatch.setattr(_uringcore, "EventFD", fail_eventfd)
    with pytest.raises(PermissionError, match="injected eventfd failure"):
        IoUringProactorEventLoop(proactor, thread_wakeup="eventfd")

    assert proactor.closed is True


def test_eventfd_poll_failure_falls_back_to_signal_socketpair():
    proactor = ConstructorProactor()
    loop = IoUringProactorEventLoop(proactor, thread_wakeup="eventfd")
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    wakeup = loop._eventfd_wakeup
    future = loop._eventfd_poll_future
    try:
        future.set_exception(OSError("injected poll completion failure"))
        loop._run_once()

        assert loop._eventfd_wakeup is None
        assert loop._eventfd_poll_future is None
        assert loop._thread_wakeup_mode == "socketpair"
        assert wakeup.closed is True
        assert errors[0]["message"] == "eventfd wakeup poll failed"

        loop.call_soon_threadsafe(lambda: None)
        assert loop._eventfd_wakeup is None
    finally:
        loop.close()
