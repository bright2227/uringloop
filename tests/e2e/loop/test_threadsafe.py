import os
import signal
import threading
import time

import pytest

from uringloop.loop import IoUringProactorEventLoop


@pytest.mark.parametrize("thread_wakeup", ["socketpair", "eventfd"])
def test_call_soon_threadsafe_wakes_idle_native_ring(thread_wakeup):
    loop = IoUringProactorEventLoop(thread_wakeup=thread_wakeup)
    callback_ran = threading.Event()

    def callback():
        callback_ran.set()
        loop.stop()

    def wake_loop():
        time.sleep(0.02)
        loop.call_soon_threadsafe(callback)

    thread = threading.Thread(target=wake_loop)
    loop.call_later(1, loop.stop)
    started = time.monotonic()
    thread.start()
    try:
        loop.run_forever()
    finally:
        thread.join(timeout=1)
        loop.close()

    assert callback_ran.is_set()
    assert time.monotonic() - started < 0.5


def test_eventfd_wakeup_coalesces_many_producers_without_losing_callbacks():
    loop = IoUringProactorEventLoop(thread_wakeup="eventfd")
    producer_count = 8
    callbacks_per_producer = 100
    expected = producer_count * callbacks_per_producer
    completed = 0

    def callback():
        nonlocal completed
        completed += 1
        if completed == expected:
            loop.stop()

    def producer():
        time.sleep(0.02)
        for _ in range(callbacks_per_producer):
            loop.call_soon_threadsafe(callback)

    threads = [threading.Thread(target=producer) for _ in range(producer_count)]
    loop.call_later(2, loop.stop)
    for thread in threads:
        thread.start()
    try:
        loop.run_forever()
    finally:
        for thread in threads:
            thread.join(timeout=1)
        loop.close()

    assert completed == expected


def test_eventfd_thread_wakeup_keeps_signal_socketpair_active():
    loop = IoUringProactorEventLoop(thread_wakeup="eventfd")
    received = []

    def signal_received():
        received.append(signal.SIGUSR1)
        loop.stop()

    def send_signal():
        time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGUSR1)

    loop.add_signal_handler(signal.SIGUSR1, signal_received)
    sender = threading.Thread(target=send_signal)
    loop.call_later(2, loop.stop)
    sender.start()
    try:
        loop.run_forever()
    finally:
        sender.join(timeout=1)
        loop.remove_signal_handler(signal.SIGUSR1)
        loop.close()

    assert received == [signal.SIGUSR1]


def test_eventfd_wakeup_after_close_uses_closed_loop_error():
    loop = IoUringProactorEventLoop(thread_wakeup="eventfd")
    loop.close()

    with pytest.raises(RuntimeError, match="closed"):
        loop.call_soon_threadsafe(lambda: None)
