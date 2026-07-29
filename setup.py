from setuptools import Extension, setup


setup(
    cffi_modules=["_ffi_build.py:ffibuilder"],
    ext_modules=[
        Extension(
            "uringloop._uringcore_liburing",
            sources=["src/uringcore_liburing.c"],
            include_dirs=["libs/src/include"],
            extra_objects=["libs/src/liburing.a"],
        ),
    ],
)
