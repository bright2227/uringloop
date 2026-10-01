__all__ = [
    "IoUringProactor",
    "IoUringProactorEventLoop",
    "IoUringProactorEventLoopPolicy",
    # deprecated aliases (0.1.x naming)
    "IouringProactorEventLoop",
    "IouringProactorEventLoopPolicy",
]


import platform
import re

from uringloop.loop import (
    IoUringProactorEventLoop,
    IoUringProactorEventLoopPolicy,
    IouringProactorEventLoop,
    IouringProactorEventLoopPolicy,
)
from uringloop.proactor import IoUringProactor


MIN_KERNEL_VERSION = (5, 19)


def check_kernel_version():
    if platform.system() != 'Linux':
        raise RuntimeError("Only supported on Linux")
    release = platform.release()
    matched = re.match(r"(\d+)\.(\d+)", release)
    if matched is None:
        raise RuntimeError(f"Unable to parse Linux kernel version from {release!r}")
    major, minor = int(matched.group(1)), int(matched.group(2))
    if (major, minor) < MIN_KERNEL_VERSION:
        raise RuntimeError(
            f"Linux kernel {MIN_KERNEL_VERSION[0]}.{MIN_KERNEL_VERSION[1]}+ required (found {major}.{minor})"
        )

check_kernel_version()
