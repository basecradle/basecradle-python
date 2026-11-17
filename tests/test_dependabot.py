"""The ``publish-path`` Dependabot group names exactly the build toolchain uv.lock holds (#346).

The shared auto-merge (``basecradle/.github``) sends an update in a group named
``publish-path`` to the capital instead of merging it (basecradle#874). This repository says
what that group holds, in ``.github/dependabot.yml``: the ``build`` dependency group and its
whole closure in ``uv.lock``, the packages that make the published bytes. A list typed by
hand goes stale the day a toolchain bump brings in a new dependency, and a name it misses
merges unread. So the list is held equal to a walk of the lock: a name the walk finds and the
list lacks fails, and so does a name the list keeps after the lock dropped it.

Both files are read narrowly, failing closed on any shape the reader does not know. The lock
is read with ``tomllib``, standard library from 3.11, so the 3.10 leg skips the comparison
(as ``tests/test_workflows.py`` does) rather than add ``tomli`` for one test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from scripts.locked_requirement import locked_package

ROOT = Path(__file__).resolve().parent.parent
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
LOCK = ROOT / "uv.lock"
PROJECT = "basecradle"
BUILD_GROUP = "build"
GROUP = "publish-path"

#: A pattern Dependabot reads as one exact package name: no wildcard, no quoting games.
EXACT_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _toml(text: str) -> dict[str, Any]:
    tomllib = pytest.importorskip("tomllib", reason="tomllib is standard library from 3.11")
    parsed: dict[str, Any] = tomllib.loads(text)
    return parsed


def build_closure(lock_text: str) -> set[str]:
    """Every package the project's ``build`` group reaches in the lock, markers included.

    A dependency behind a marker (``tomli`` below Python 3.11) is in: whether it installs
    depends on the interpreter, and the list must cover every interpreter the build may use.
    Each package reached must be one PyPI entry with its hashes (``locked_package``), or the
    walk raises ``ValueError``: it cannot tell which of two entries builds.
    """
    lock = _toml(lock_text)
    projects = [package for package in lock.get("package", []) if package["name"] == PROJECT]
    assert len(projects) == 1, f"uv.lock must hold one `{PROJECT}` to start the walk from"
    start = projects[0].get("dev-dependencies", {}).get(BUILD_GROUP)
    assert start, f"uv.lock's `{PROJECT}` has no `{BUILD_GROUP}` group: nothing to walk"

    walked: set[tuple[str, str | None]] = set()
    pending = list(start)
    while pending:
        dependency = pending.pop()
        name = dependency["name"]
        for extra in [None, *dependency.get("extra", [])]:
            if (name, extra) in walked:
                continue
            walked.add((name, extra))
            package = locked_package(lock, name)
            if extra is None:
                pending.extend(package.get("dependencies", []))
            else:
                pending.extend(package.get("optional-dependencies", {}).get(extra, []))
    return {name for name, _ in walked}


def publish_path_patterns(text: str) -> list[str]:
    """The ``patterns`` of the uv ecosystem's ``publish-path`` group, read line by line.

    Fails closed on what would narrow it unseen: another group before it (Dependabot puts a
    package in the first group it matches), any key in the group but ``patterns``
    (``exclude-patterns``; ``dependency-type: production``, since every build package is a
    dev dependency), and an ``allow`` or ``ignore`` on the uv entry.
    """
    entries = re.split(r"(?m)^(?=- package-ecosystem:)", text)
    uv = [e for e in entries if re.match(r"- package-ecosystem: uv\s*$", e, re.MULTILINE)]
    assert len(uv) == 1, f"dependabot.yml must have exactly one uv entry, not {len(uv)}"
    lines = [
        line for line in uv[0].splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    narrowing = [line for line in lines if re.match(r"  (allow|ignore):", line)]
    assert not narrowing, (
        f"the uv entry's {narrowing} can drop build packages from Dependabot's updates, and "
        f"this reader cannot tell whether it does"
    )
    groups = [i for i, line in enumerate(lines) if line == "  groups:"]
    assert len(groups) == 1 and lines[groups[0] + 1 : groups[0] + 2] == [f"    {GROUP}:"], (
        f"the uv entry's `groups:` must open with one `{GROUP}` group, ahead of any other"
    )

    body: list[str] = []
    for line in lines[groups[0] + 2 :]:
        if not line.startswith("      "):
            break
        body.append(line)
    assert body and body[0] == "      patterns:", (
        f"the `{GROUP}` group must open with `patterns:`, and hold nothing else"
    )
    patterns = []
    for line in body[1:]:
        match = re.fullmatch(r"      - (\S+)", line)
        assert match, f"the `{GROUP}` group holds a line its reader cannot vouch for: {line!r}"
        patterns.append(match.group(1))
    return patterns


def check_publish_path_matches_the_lock(dependabot_text: str, lock_text: str) -> None:
    patterns = publish_path_patterns(dependabot_text)
    inexact = [pattern for pattern in patterns if not EXACT_NAME.match(pattern)]
    assert not inexact, (
        f"every `{GROUP}` pattern must be one exact, normalized package name: {inexact}"
    )
    assert len(patterns) == len(set(patterns)), f"`{GROUP}` names a package twice: {patterns}"
    closure = build_closure(lock_text)
    missing = sorted(closure - set(patterns))
    extra = sorted(set(patterns) - closure)
    assert not missing and not extra, (
        f"the `{GROUP}` Dependabot group must name exactly the `{BUILD_GROUP}` group's closure "
        f"in uv.lock (#346), or a bump of a build package merges without the capital reading "
        f"it.\n  in the lock, missing from the group: {missing}\n"
        f"  in the group, gone from the lock: {extra}"
    )


def test_the_publish_path_group_names_exactly_the_locked_build_toolchain():
    check_publish_path_matches_the_lock(
        DEPENDABOT.read_text(encoding="utf-8"), LOCK.read_text(encoding="utf-8")
    )


# --- the readers themselves, on fabricated inputs ---------------------------------------


def _package(name: str, *dependencies: str, **tables: str) -> str:
    deps = ", ".join(f'{{ name = "{dependency}" }}' for dependency in dependencies)
    extra = "".join(f"\n[package.{key}]\n{value}\n" for key, value in tables.items())
    return (
        f'[[package]]\nname = "{name}"\nversion = "1.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        f"dependencies = [{deps}]\n"
        f'wheels = [{{ url = "https://example.com/{name}.whl", hash = "sha256:{"b" * 64}" }}]\n'
        f"{extra}\n"
    )


LOCK_TEXT = (
    _package(PROJECT, **{"dev-dependencies": 'build = [{ name = "backend" }]'})
    + _package("backend", "helper")
    + _package("helper")
    + _package("unrelated")
)


def _dependabot(*patterns: str, more: str = "") -> str:
    items = "".join(f"      - {pattern}\n" for pattern in patterns)
    return (
        "version: 2\nupdates:\n- package-ecosystem: uv\n  directory: /\n  groups:\n"
        f"    {GROUP}:\n      patterns:\n{items}{more}"
        "  open-pull-requests-limit: 10\n"
        "- package-ecosystem: github-actions\n  directory: /\n  groups:\n"
        "    github-actions:\n      patterns:\n      - '*'\n"
    )


class TestTheReaders:
    def test_accepts_a_group_equal_to_the_closure(self):
        check_publish_path_matches_the_lock(_dependabot("backend", "helper"), LOCK_TEXT)

    def test_walks_past_the_first_level(self):
        assert build_closure(LOCK_TEXT) == {"backend", "helper"}

    def test_follows_an_extra(self):
        lock = (
            _package(PROJECT, **{"dev-dependencies": 'build = [{ name = "backend", extra = ["x"] }]'})
            + _package("backend", **{"optional-dependencies": 'x = [{ name = "plugin" }]'})
            + _package("plugin")
        )  # fmt: skip
        assert build_closure(lock) == {"backend", "plugin"}

    @pytest.mark.parametrize(
        ("patterns", "reason"),
        [
            (["backend"], "missing from the group: ['helper']"),
            (["backend", "helper", "unrelated"], "gone from the lock: ['unrelated']"),
            (["backend", "helper", "*"], "one exact, normalized package name"),
            (["backend", "helper", "helper"], "names a package twice"),
            (["Backend", "helper"], "one exact, normalized package name"),
        ],
    )
    def test_refuses_a_group_that_differs_from_the_closure(self, patterns, reason):
        with pytest.raises(AssertionError, match=re.escape(reason)):
            check_publish_path_matches_the_lock(_dependabot(*patterns), LOCK_TEXT)

    @pytest.mark.parametrize(
        "more",
        [
            "      exclude-patterns:\n      - helper\n",
            "      dependency-type: production\n",
        ],
    )
    def test_refuses_a_key_that_narrows_the_group(self, more):
        with pytest.raises(AssertionError, match="cannot vouch for"):
            publish_path_patterns(_dependabot("backend", "helper", more=more))

    def test_follows_a_cycle_of_extras_to_its_end(self):
        lock = (
            _package(PROJECT, **{"dev-dependencies": 'build = [{ name = "a", extra = ["x"] }]'})
            + _package(
                "a",
                **{"optional-dependencies": 'x = [{ name = "a", extra = ["y"] }]\ny = [{ name = "a", extra = ["x"] }]'},
            )
        )  # fmt: skip
        assert build_closure(lock) == {"a"}

    def test_a_package_twice_outside_the_closure_is_left_alone(self):
        assert build_closure(LOCK_TEXT + _package("unrelated")) == {"backend", "helper"}

    def test_refuses_a_package_twice_inside_the_closure(self):
        with pytest.raises(ValueError, match="2 `helper` packages"):
            build_closure(LOCK_TEXT + _package("helper"))

    def test_refuses_a_build_package_the_lock_holds_without_hashes(self):
        lock = LOCK_TEXT.replace(
            _package("helper"), _package("helper").split("wheels = ")[0] + "\n"
        )
        with pytest.raises(ValueError, match="no sha256"):
            build_closure(lock)

    def test_a_narrowing_key_after_a_blank_line_is_still_seen(self):
        text = _dependabot("backend", "helper", more="\n      exclude-patterns:\n      - helper\n")
        with pytest.raises(AssertionError, match="cannot vouch for"):
            publish_path_patterns(text)

    def test_refuses_a_group_ahead_of_it(self):
        text = _dependabot("backend", "helper").replace(
            "  groups:\n", "  groups:\n    everything:\n      patterns:\n      - '*'\n", 1
        )
        with pytest.raises(AssertionError, match="ahead of any other"):
            publish_path_patterns(text)

    @pytest.mark.parametrize("key", ["allow", "ignore"])
    def test_refuses_an_entry_that_may_drop_build_packages(self, key):
        text = _dependabot("backend", "helper").replace(
            "  directory: /\n", f"  directory: /\n  {key}:\n  - dependency-name: helper\n", 1
        )
        with pytest.raises(AssertionError, match="can drop build packages"):
            publish_path_patterns(text)

    def test_a_group_only_in_the_actions_entry_is_not_found(self):
        text = _dependabot("backend").replace(f"    {GROUP}:", "    other:")
        text += f"    {GROUP}:\n      patterns:\n      - backend\n"
        with pytest.raises(AssertionError, match="ahead of any other"):
            publish_path_patterns(text)
