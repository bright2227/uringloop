"""Verify that an installed wheel is self-contained and importable."""

from importlib import import_module
from pathlib import Path
import shutil
import subprocess


EXTENSION_MODULES = (
    "uringloop._liburing",
    "uringloop._uringcore_liburing",
)


def dynamic_section(module_name: str) -> str:
    module = import_module(module_name)
    module_file = getattr(module, "__file__", None)
    if module_file is None:
        raise RuntimeError(f"{module_name} does not have an extension module path")

    readelf = shutil.which("readelf")
    if readelf is None:
        raise RuntimeError("readelf is required to verify wheel dependencies")

    result = subprocess.run(
        [readelf, "-d", Path(module_file)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def main() -> None:
    for module_name in EXTENSION_MODULES:
        dependencies = dynamic_section(module_name)
        if "liburing.so" in dependencies:
            raise RuntimeError(f"{module_name} dynamically links liburing")
        print(f"verified {module_name}")


if __name__ == "__main__":
    main()
