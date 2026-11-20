"""``scripts/locked_requirement.py``: the line the release build installs uv from (#346).

What it refuses (no uv, two, one from elsewhere, one without sha256s) is pinned in
``tests/test_workflows.py`` beside the checks that read the real lock through it. Here: that
the line it prints carries every hash the lock holds, and that its command line fails loudly.
"""

from __future__ import annotations

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


def test_it_prints_the_real_locks_uv(capsys):
    assert locked_requirement.main(["uv"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("uv==") and " --hash=sha256:" in line


def test_a_package_the_lock_lacks_fails_with_the_reason(capsys):
    assert locked_requirement.main(["no-such-package"]) == 1
    assert "holds 0 `no-such-package` packages" in capsys.readouterr().err


@pytest.mark.parametrize("version", [None, ""])
def test_an_entry_with_no_version_is_refused_with_the_reason(version):
    (uv, _) = LOCK["package"]
    entry = {key: value for key, value in uv.items() if key != "version"}
    if version is not None:
        entry["version"] = version
    with pytest.raises(ValueError, match="has no version"):
        requirement({"package": [entry]}, "uv")
