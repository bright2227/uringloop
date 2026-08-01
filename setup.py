import os
from pathlib import Path
import shutil
import subprocess

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


ROOT = Path(__file__).resolve().parent
LIBURING_SOURCE = ROOT / "libs"
LIBURING_EXTENSION_NAMES = {
    "uringloop._liburing",
    "uringloop._uringcore",
}


class VendoredLiburingBuildExt(build_ext):
    """Build one private liburing archive for extensions that need it."""

    def build_extensions(self):
        extensions = [extension for extension in self.extensions if extension.name in LIBURING_EXTENSION_NAMES]
        if extensions:
            archive = self._build_vendored_liburing()
            include_directory = archive.parent / "include"
            for extension in extensions:
                extension.extra_objects = [*(extension.extra_objects or []), str(archive)]
                extension.depends = [*(extension.depends or []), str(archive)]
                extension.include_dirs = [str(include_directory)]

        super().build_extensions()

    def _build_vendored_liburing(self):
        build_root = Path(self.build_temp).resolve() / "vendored-liburing"
        if build_root.exists():
            shutil.rmtree(build_root)

        self.announce(f"copying vendored liburing sources to {build_root}", level=2)
        shutil.copytree(
            LIBURING_SOURCE,
            build_root,
            ignore=shutil.ignore_patterns(
                ".git",
                "*.a",
                "*.d",
                "*.o",
                "*.ol",
                "*.os",
                "*.so",
                "*.so.*",
                "examples",
                "man",
                "test",
            ),
        )

        environment = os.environ.copy()
        compiler_command = getattr(self.compiler, "compiler", None)
        if compiler_command:
            environment.setdefault("CC", compiler_command[0])
        liburing_cflags = environment.get("LIBURING_CFLAGS", "")
        environment["LIBURING_CFLAGS"] = f"{liburing_cflags} -fPIC".strip()

        self.announce("configuring vendored liburing", level=2)
        subprocess.run(
            ["sh", "configure", "--use-libc"],
            cwd=build_root,
            env=environment,
            check=True,
        )
        self.announce("building vendored static liburing", level=2)
        subprocess.run(
            ["make", "-C", "src", "liburing.a"],
            cwd=build_root,
            env=environment,
            check=True,
        )
        return build_root / "src" / "liburing.a"


setup(
    cffi_modules=["_ffi_build.py:ffibuilder"],
    cmdclass={"build_ext": VendoredLiburingBuildExt},
    ext_modules=[
        Extension(
            "uringloop._uringcore",
            sources=["src/uringcore_liburing.c"],
        ),
    ],
)
