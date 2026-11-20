"""The ``publish-path`` Dependabot group names exactly the build toolchain uv.lock holds (#346).

The shared auto-merge (``basecradle/.github``) sends an update in a group named
``publish-path`` to the capital instead of merging it (basecradle#874). This repository says
what that group holds, in ``.github/dependabot.yml``: the ``build`` dependency group and its
whole closure in ``uv.lock``, the packages that make the published bytes. A list typed by
hand goes stale the day a toolchain bump brings in a new dependency, and a name it misses
merges unread. So the list is held equal to a walk of the lock: a name the walk finds and the
list lacks fails, and so does a name the list keeps after the lock dropped it.

Both files are read narrowly, failing closed on any shape the reader does not know.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from scripts.locked_requirement import locked_package

ROOT = Path(__file__).resolve().parent.parent
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
LOCK = ROOT / "uv.lock"
PROJECT = "basecradle"
BUILD_GROUP = "build"
GROUP = "publish-path"
#: What the uv entry ignores: uv, which moves with setup-uv instead (#373).
IGNORED = {"uv"}

#: A pattern Dependabot reads as one exact package name: no wildcard, no quoting games.
EXACT_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def build_closure(lock_text: str) -> set[str]:
    """Every package the project's ``build`` group reaches in the lock, markers included.

    A dependency behind an environment marker is in: whether it installs
    depends on the interpreter, and the list must cover every interpreter the build may use.
    Each package reached must be one PyPI entry with its hashes (``locked_package``), or the
    walk raises ``ValueError``: it cannot tell which of two entries builds.
    """
    lock = tomllib.loads(lock_text)
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


def _uv_entry_lines(text: str) -> list[str]:
    """The uv ecosystem's entry, its blank and comment lines dropped."""
    entries = re.split(r"(?m)^(?=- package-ecosystem:)", text)
    uv = [e for e in entries if re.match(r"- package-ecosystem: uv\s*$", e, re.MULTILINE)]
    assert len(uv) == 1, f"dependabot.yml must have exactly one uv entry, not {len(uv)}"
    return [
        line for line in uv[0].splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def uv_entry_ignores(text: str) -> list[str]:
    """The package names the uv entry's ``ignore`` drops whole, read line by line.

    Each item must be a bare ``- dependency-name: <name>``: a ``versions`` or
    ``update-types`` beside it drops only some updates, and this reader cannot say which.
    """
    lines = _uv_entry_lines(text)
    starts = [i for i, line in enumerate(lines) if line == "  ignore:"]
    stray = [line for line in lines if re.match(r"  ignore:", line) and line != "  ignore:"]
    assert len(starts) <= 1 and not stray, "the uv entry's `ignore:` is not one readable list"
    names = []
    for line in lines[starts[0] + 1 :] if starts else []:
        if not line.startswith(("  - ", "    ")):
            break
        match = re.fullmatch(r"  - dependency-name: (\S+)", line)
        assert match, f"the uv entry's `ignore` holds a line its reader cannot vouch for: {line!r}"
        names.append(match.group(1))
    return names


def publish_path_patterns(text: str) -> list[str]:
    """The ``patterns`` of the uv ecosystem's ``publish-path`` group, read line by line.

    Fails closed on what would narrow it unseen: another group before it (Dependabot puts a
    package in the first group it matches), any key in the group but ``patterns``
    (``exclude-patterns``; ``dependency-type: production``, since every build package is a
    dev dependency), and an ``allow`` on the uv entry. Its ``ignore`` is read by
    ``uv_entry_ignores`` and held to ``IGNORED``.
    """
    lines = _uv_entry_lines(text)
    narrowing = [line for line in lines if re.match(r"  allow:", line)]
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


def check_the_ignore_is_uv_alone(dependabot_text: str) -> None:
    """Dependabot proposes every build package but uv, and uv stays in the group (#373).

    uv moves with setup-uv instead: drift-guard fails a setup-uv bump until the newest uv its
    checksum table holds is locked (``tests/test_workflows.py``), and every action bump here
    is the capital's to read. Any other name ignored is a build package nothing would move.
    The ignored name stays in ``publish-path``, so the day the ignore is lifted its bumps are
    routed to the capital again rather than merged unread, and a uv that leaves the build
    toolchain takes the ignore with it rather than leaving it behind, ignoring nothing.
    """
    ignored = uv_entry_ignores(dependabot_text)
    assert ignored == sorted(IGNORED), (
        f"the uv entry must ignore exactly {sorted(IGNORED)}, which moves with setup-uv "
        f"(#373), and nothing else, which nothing would move: it ignores {ignored}"
    )
    patterns = publish_path_patterns(dependabot_text)
    assert set(ignored) <= set(patterns), (
        f"an ignored package must stay in `{GROUP}`, so a bump of it is still read: "
        f"{sorted(set(ignored) - set(patterns))}"
    )


def test_the_publish_path_group_names_exactly_the_locked_build_toolchain():
    check_publish_path_matches_the_lock(
        DEPENDABOT.read_text(encoding="utf-8"), LOCK.read_text(encoding="utf-8")
    )


def test_dependabot_proposes_no_uv_and_the_group_still_names_it():
    check_the_ignore_is_uv_alone(DEPENDABOT.read_text(encoding="utf-8"))


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

    def test_refuses_an_allow_that_may_drop_build_packages(self):
        text = _dependabot("backend", "helper").replace(
            "  directory: /\n", "  directory: /\n  allow:\n  - dependency-name: helper\n", 1
        )
        with pytest.raises(AssertionError, match="can drop build packages"):
            publish_path_patterns(text)

    @staticmethod
    def _ignoring(*items: str, patterns: tuple[str, ...] = ("backend", "uv")) -> str:
        return _dependabot(*patterns).replace(
            "  directory: /\n", "  directory: /\n  ignore:\n" + "".join(items), 1
        )

    def test_accepts_an_ignore_of_uv_alone(self):
        check_the_ignore_is_uv_alone(self._ignoring("  - dependency-name: uv\n"))

    @pytest.mark.parametrize(
        "items",
        [
            (),
            ("  - dependency-name: helper\n",),
            ("  - dependency-name: uv\n", "  - dependency-name: backend\n"),
        ],
    )
    def test_refuses_an_ignore_other_than_uv_alone(self, items):
        with pytest.raises(AssertionError, match="must ignore exactly"):
            check_the_ignore_is_uv_alone(self._ignoring(*items))

    def test_refuses_no_ignore_at_all(self):
        with pytest.raises(AssertionError, match="must ignore exactly"):
            check_the_ignore_is_uv_alone(_dependabot("backend", "uv"))

    def test_refuses_an_ignored_uv_the_group_no_longer_names(self):
        text = self._ignoring("  - dependency-name: uv\n", patterns=("backend",))
        with pytest.raises(AssertionError, match="must stay in"):
            check_the_ignore_is_uv_alone(text)

    @pytest.mark.parametrize(
        "items",
        [
            ("  - dependency-name: uv\n", "    versions: ['>=1']\n"),
            ("  - dependency-name: uv\n", "    update-types: ['version-update:semver-major']\n"),
            ("  - dependency-name: 'uv'\n",),
            ("  - dependency-name: uv*\n", "  - {dependency-name: uv}\n"),
        ],
    )
    def test_refuses_an_ignore_item_it_cannot_vouch_for(self, items):
        with pytest.raises(AssertionError, match="cannot vouch for|must ignore exactly"):
            check_the_ignore_is_uv_alone(self._ignoring(*items))

    def test_refuses_a_second_or_inline_ignore(self):
        twice = self._ignoring("  - dependency-name: uv\n", "  ignore:\n  - dependency-name: uv\n")
        inline = self._ignoring().replace("  ignore:\n", "  ignore: [{dependency-name: uv}]\n")
        for text in (twice, inline):
            with pytest.raises(AssertionError, match="one readable list"):
                uv_entry_ignores(text)

    def test_an_ignore_in_the_actions_entry_is_not_the_uv_entrys(self):
        text = _dependabot("backend", "uv") + "  ignore:\n  - dependency-name: uv\n"
        assert uv_entry_ignores(text) == []

    def test_a_group_only_in_the_actions_entry_is_not_found(self):
        text = _dependabot("backend").replace(f"    {GROUP}:", "    other:")
        text += f"    {GROUP}:\n      patterns:\n      - backend\n"
        with pytest.raises(AssertionError, match="ahead of any other"):
            publish_path_patterns(text)
