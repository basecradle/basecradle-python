"""Print one package of ``uv.lock`` as a hash-pinned requirement (#346).

The release build installs uv itself with this, before any uv exists to read the lock:
``pip install --require-hashes --no-deps --only-binary :all: -r <file>`` then accepts only a
wheel whose sha256 the lock records, so the uv that builds the published bytes is held by a
hash in this repository's own reviewed diff, and Dependabot's uv entry moves it.
``astral-sh/setup-uv`` cannot give that: it downloads uv's GitHub release archive, and for a
uv newer than the action's built-in checksum table it checks that archive against a checksum
fetched at run time from the same host.

Standard library only, run with the runner's own ``python3`` (3.11 or newer, for
``tomllib``)::

    python3 scripts/locked_requirement.py uv

A port of basecradle-harness's script of the same name (basecradle-harness#916).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

LOCK = Path(__file__).resolve().parent.parent / "uv.lock"

#: The one index this repository resolves from. A package from anywhere else -- a git
#: source, a path, another index -- is not one the hashes below were checked against.
PYPI = {"registry": "https://pypi.org/simple"}


def requirement(lock: dict[str, Any], name: str) -> str:
    """``name==<locked version>`` with every hash the lock holds for it, or a ValueError."""
    packages = [package for package in lock.get("package", []) if package.get("name") == name]
    if len(packages) != 1:
        raise ValueError(f"uv.lock holds {len(packages)} `{name}` packages, not one")
    (package,) = packages
    if package.get("source") != PYPI:
        raise ValueError(f"uv.lock's `{name}` is not from PyPI: {package.get('source')}")
    version = package.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"uv.lock's `{name}` has no version: {version!r}")
    files = [*package.get("wheels", []), *([package["sdist"]] if "sdist" in package else [])]
    hashes = [str(file.get("hash", "")) for file in files]
    if not hashes or not all(value.startswith("sha256:") for value in hashes):
        raise ValueError(f"uv.lock's `{name}` carries no sha256 for every file")
    return " ".join([f"{name}=={version}", *(f"--hash={value}" for value in hashes)])


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: locked_requirement.py <package>", file=sys.stderr)
        return 2
    # tomllib is standard library from 3.11, and this repository's floor is 3.10, so a 3.10
    # interpreter says why rather than failing on the import. Written as one version branch
    # because that is the form mypy, which checks scripts/ as 3.10, resolves before it
    # resolves the import.
    if sys.version_info >= (3, 11):
        import tomllib

        try:
            print(requirement(tomllib.loads(LOCK.read_text(encoding="utf-8")), argv[0]))
        except ValueError as error:
            print(f"locked_requirement: {error}", file=sys.stderr)
            return 1
        return 0
    print("locked_requirement: needs Python 3.11 or newer (tomllib)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
