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
SANITIZER_ENVIRONMENT_VARIABLE = "URINGLOOP_SANITIZERS"


def sanitizer_flags():
    """Return compiler and linker flags for an opt-in sanitizer build."""
    requested = os.environ.get(SANITIZER_ENVIRONMENT_VARIABLE, "").strip()
    if not requested:
        return []

    sanitizers = [sanitizer.strip() for sanitizer in requested.split(",") if sanitizer.strip()]
    supported = {"address", "undefined"}
    unsupported = sorted(set(sanitizers) - supported)
    if unsupported:
        unsupported_list = ", ".join(unsupported)
        raise RuntimeError(
            f"unsupported {SANITIZER_ENVIRONMENT_VARIABLE} value(s): {unsupported_list}; "
            f"supported values are: {', '.join(sorted(supported))}"
        )
    if not sanitizers:
        raise RuntimeError(f"{SANITIZER_ENVIRONMENT_VARIABLE} must name at least one sanitizer")

    return [
        f"-fsanitize={','.join(dict.fromkeys(sanitizers))}",
        "-fno-omit-frame-pointer",
        "-fno-sanitize-recover=all",
    ]


class VendoredLiburingBuildExt(build_ext):
    """Build one private liburing archive for extensions that need it."""

    def build_extensions(self):
        extensions = [extension for extension in self.extensions if extension.name in LIBURING_EXTENSION_NAMES]
        if extensions:
            instrumentation_flags = sanitizer_flags()
            archive = self._build_vendored_liburing(instrumentation_flags)
            include_directory = archive.parent / "include"
            for extension in extensions:
                extension.extra_objects = [*(extension.extra_objects or []), str(archive)]
                extension.depends = [*(extension.depends or []), str(archive)]
                extension.include_dirs = [str(include_directory)]
                extension.extra_compile_args = [*(extension.extra_compile_args or []), *instrumentation_flags]
                extension.extra_link_args = [*(extension.extra_link_args or []), *instrumentation_flags]

        super().build_extensions()

    def _build_vendored_liburing(self, instrumentation_flags):
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
        environment["LIBURING_CFLAGS"] = " ".join([liburing_cflags, "-fPIC", *instrumentation_flags]).strip()

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
