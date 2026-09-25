from setuptools import find_packages, setup

setup(
    name="regulatory-triage-core",
    version="0.1.0",
    description="基层环保监管资料服务",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
