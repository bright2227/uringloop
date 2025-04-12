from setuptools import setup


setup(
    cffi_modules=["ffi.py:ffibuilder"],
    py_modules=["ffi"],
    include_package_data=True
)