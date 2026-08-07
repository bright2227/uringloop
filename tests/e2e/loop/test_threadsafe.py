import threading
import time

from uringloop.loop import IoUringProactorEventLoop


def test_call_soon_threadsafe_wakes_idle_native_ring():
    loop = IoUringProactorEventLoop()
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
