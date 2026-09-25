from setuptools import find_packages, setup

setup(
    name="festival-foundation",
    version="0.1.0",
    description="节日公共服务协作基础层",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
