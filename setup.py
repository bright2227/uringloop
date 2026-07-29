from setuptools import Extension, setup


setup(
    cffi_modules=["_ffi_build.py:ffibuilder"],
    ext_modules=[
        Extension(
            "uringloop._uringcore",
            sources=["src/uringcore.c"],
        )
    ],
)
