from glob import glob
from setuptools import find_packages, setup

setup(
    name="vln_cli",
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/vln_cli"]),
        ("share/vln_cli", ["package.xml", "README.md", "requirements.txt"]),
        ("share/vln_cli/config", glob("config/*.yaml")),
    ],
    package_data={"vln_cli": ["*.tcss"]},
    install_requires=["setuptools", "textual>=8.2,<9"],
    tests_require=["pytest"],
    extras_require={"test": ["pytest>=8.2,<9", "pytest-asyncio"]},
    zip_safe=False,
    maintainer="yan",
    maintainer_email="yan@todo.todo",
    description="A terminal control room for VLN Agent.",
    license="Apache-2.0",
    entry_points={"console_scripts": ["vln_cli = vln_cli.main:main"]},
)
