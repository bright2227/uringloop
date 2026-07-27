from collections.abc import Buffer
from typing import Any, TypeAlias


IoUring = Any
IoUringSqe = Any
IoUringCqe = Any
KernelTimespec = Any
Iovec = Any
SockaddrIn = Any
SockaddrIn6 = Any
SockaddrUn = Any
SocklenT = Any
Sockaddr = SockaddrIn | SockaddrIn6 | SockaddrUn
pyAddress: TypeAlias = tuple[Any, ...] | str | Buffer
