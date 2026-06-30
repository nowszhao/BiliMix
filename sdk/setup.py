from setuptools import setup

setup(
    name="bilimix-cli",
    version="1.0.0",
    description="BiliMix CLI — 面向 AI Agent 的命令行接口与 Python SDK",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="BiliMix",
    license="MIT",
    python_requires=">=3.8",
    install_requires=[
        "requests>=2.28",
    ],
    packages=["bilimix_cli"],
    entry_points={
        "console_scripts": [
            "bmx = bilimix_cli.cli:main",
        ],
    },
    classifiers=[
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
