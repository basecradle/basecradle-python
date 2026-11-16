"""``scripts/locked_requirement.py``: the line the release build installs uv from (#346).

What it refuses (no uv, two, one from elsewhere, one without sha256s) is pinned in
``tests/test_workflows.py`` beside the checks that read the real lock through it. Here: that
the line it prints carries every hash the lock holds, and that its command line fails loudly.
"""

from __future__ import annotations

import sys

import pytest

from scripts import locked_requirement
from scripts.locked_requirement import requirement

#: Two files, so a requirement that dropped either hash is visible. Fabricated.
LOCK = {
    "package": [
        {
            "name": "uv",
            "version": "0.13.6",
            "source": {"registry": "https://pypi.org/simple"},
            "sdist": {"url": "https://example.com/uv.tar.gz", "hash": "sha256:" + "a" * 64},
            "wheels": [{"url": "https://example.com/uv.whl", "hash": "sha256:" + "b" * 64}],
        },
        {"name": "hatchling", "version": "1.33.0"},
    ]
}


def test_the_requirement_carries_every_hash_the_lock_holds():
    assert requirement(LOCK, "uv") == (
        f"uv==0.13.6 --hash=sha256:{'b' * 64} --hash=sha256:{'a' * 64}"
    )


def test_a_wheel_only_package_needs_no_sdist():
    (uv, _) = LOCK["package"]
    lock = {"package": [{key: value for key, value in uv.items() if key != "sdist"}]}
    assert requirement(lock, "uv") == f"uv==0.13.6 --hash=sha256:{'b' * 64}"


@pytest.mark.parametrize("argv", [[], ["uv", "hatchling"]])
def test_anything_but_one_package_is_a_usage_error(argv, capsys):
    assert locked_requirement.main(argv) == 2
    assert "usage" in capsys.readouterr().err


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is standard library from 3.11")
def test_it_prints_the_real_locks_uv(capsys):
    assert locked_requirement.main(["uv"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("uv==") and " --hash=sha256:" in line


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is standard library from 3.11")
def test_a_package_the_lock_lacks_fails_with_the_reason(capsys):
    assert locked_requirement.main(["no-such-package"]) == 1
    assert "holds 0 `no-such-package` packages" in capsys.readouterr().err


@pytest.mark.skipif(sys.version_info >= (3, 11), reason="only 3.10 lacks tomllib")
def test_on_3_10_it_says_why_it_cannot_run(capsys):
    assert locked_requirement.main(["uv"]) == 2
    assert "3.11" in capsys.readouterr().err
