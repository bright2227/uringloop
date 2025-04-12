__all__ = ["IoUringProactor", "IouringProactorEventLoopPolicy", "IouringProactorEventLoop"]


import platform

from uringloop.loop import IouringProactorEventLoop, IouringProactorEventLoopPolicy
from uringloop.proactor import IoUringProactor


def check_kernel_version():
    if platform.system() != 'Linux':
        raise RuntimeError("Only supported on Linux")
    major, minor = map(int, platform.release().split('.')[:2])
    if (major, minor) < (5, 15):
        raise RuntimeError(f"Linux kernel 5.15+ required (found {major}.{minor})")

check_kernel_version()
