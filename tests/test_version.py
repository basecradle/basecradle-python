"""The version lives in code; hatchling derives the package metadata from it.

These tests pin that wiring: if the build backend and the code ever disagree, or the
version stops being a valid PEP 440 string, they fail. They also pin ``CHANGELOG.md``,
the release's third hand-edited copy of the version, which nothing in this repo read
before #229 -- ``release.yml``'s guards compare the tag to the built artifacts, to main's
tip, and to the final-release shape, and none of them can see the notes.
"""

import importlib.metadata
import re
from pathlib import Path

import pytest

import basecradle

CHANGELOG = Path(__file__).parent.parent / "CHANGELOG.md"
CHANGELOG_TEXT = CHANGELOG.read_text(encoding="utf-8")  # em dashes: never the locale's guess

# `[0-9]` rather than `\d`, which also matches Arabic-Indic and other Unicode digits --
# the same spelling release.yml's version guard uses, so the two read alike.
_VERSION = r"[0-9]+\.[0-9]+\.[0-9]+"
_RELEASED_HEADING = rf"## \[({_VERSION})\] - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}"
_UNRELEASED_HEADING = "## [Unreleased]"

# Every line that is *shaped like* a section heading, readable or not. The anchor for
# `test_every_section_heading_is_readable`: without it, a heading style this file stops
# matching would leave every check below green while enforcing nothing.
_ANY_HEADING = re.compile(r"^## \[.*$", re.MULTILINE)
_LINK = re.compile(
    rf"^\[({_VERSION})\]: "
    rf"https://github\.com/basecradle/basecradle-python/releases/tag/v\1$",
    re.MULTILINE,
)

# main's steady state is a `.dev0`; a release PR briefly makes it a final X.Y.Z. #220
# fixed the policy that those are the only two shapes that ever ship.
BASE_VERSION = basecradle.__version__.split(".dev")[0]
IS_FINAL = re.fullmatch(_VERSION, basecradle.__version__) is not None
IS_DEV = basecradle.__version__.endswith(".dev0")

on_a_release = pytest.mark.skipif(not IS_FINAL, reason="not a final release; see the .dev0 tests")
between_releases = pytest.mark.skipif(not IS_DEV, reason="not a .dev0; see the release tests")


def section_headings(text: str) -> list[str]:
    """Every heading-shaped line, trailing whitespace stripped, in the order written."""
    return [line.rstrip() for line in _ANY_HEADING.findall(text)]


def released_versions(text: str) -> list[str]:
    """Every version with a *readable* released section heading, in the order written.

    Built from the same per-line read `newest_released_version` uses, deliberately: a
    whole-file `findall` would disagree with it about a heading carrying trailing
    whitespace -- readable to one, invisible to the other -- and the checks below compare
    the two sets against each other.
    """
    versions = []
    for line in section_headings(text):
        match = re.fullmatch(_RELEASED_HEADING, line)
        if match is not None:
            versions.append(match.group(1))
    return versions


def link_versions(text: str) -> list[str]:
    """Every version with a link definition pointing at its *own* tag."""
    return _LINK.findall(text)


def newest_released_version(text: str) -> str | None:
    """The version of the topmost *released* section, or ``None`` if there is none.

    Reads the **first** heading-shaped line rather than the first one the regex happens
    to parse. ``re.search`` would scan forward past a heading it cannot read -- a
    single-digit day, an en dash -- and return the release *below* it, silently pointing
    every assertion here at the wrong section. That is the same class of quiet lie this
    module exists to prevent, so an unreadable topmost heading reads as ``None`` and
    `test_every_section_heading_is_readable` names the offending line.
    """
    for line in section_headings(text):
        if line == _UNRELEASED_HEADING:
            continue
        match = re.fullmatch(_RELEASED_HEADING, line)
        return None if match is None else match.group(1)
    return None


def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_version_matches_package_metadata():
    """__version__ in code is exactly what the build backend publishes."""
    assert basecradle.__version__ == importlib.metadata.version("basecradle")


def test_version_is_valid_pep_440():
    """The version string is a valid, normalized PEP 440 version."""
    pep_440 = (
        r"^([1-9][0-9]*!)?(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*"
        r"((a|b|rc)(0|[1-9][0-9]*))?"
        r"(\.post(0|[1-9][0-9]*))?"
        r"(\.dev(0|[1-9][0-9]*))?$"
    )
    assert re.match(pep_440, basecradle.__version__)


class TestChangelogExtraction:
    """The reading itself, against fixtures -- so it is exercised in every repo state.

    The live checks below are state-dependent by nature: main carries a `.dev0` for most
    of its life and a final version only during a release PR. Testing the extraction
    separately means the logic is never dark, and means a heading shape that stops being
    readable is a failure here rather than a silent pass there.
    """

    def test_reads_the_topmost_released_section(self):
        text = (
            "# Changelog\n\n## [Unreleased]\n\nNothing yet.\n\n"
            "## [0.12.0] - 2026-09-30\n\n### Changed\n\n- thing\n\n"
            "## [0.11.0] - 2026-09-29\n"
        )
        assert newest_released_version(text) == "0.12.0"
        assert released_versions(text) == ["0.12.0", "0.11.0"]

    def test_unreleased_is_not_mistaken_for_a_release(self):
        assert newest_released_version("# Changelog\n\n## [Unreleased]\n\nNothing yet.\n") is None

    @pytest.mark.parametrize(
        ("label", "heading"),
        [
            ("no date", "## [0.13.0]"),
            ("single-digit day", "## [0.13.0] - 2026-10-1"),
            ("en dash", "## [0.13.0] \u2013 2026-10-01"),
            ("not a version", "## [next] - 2026-10-01"),
        ],
    )
    def test_an_unreadable_newest_heading_never_falls_through(self, label, heading):
        """The regression that matters: it must not return the release *below* it.

        `re.search` over the whole file would skip the unreadable heading and hand back
        0.12.0 for a file whose newest section is 0.13.0 -- so the guard would compare
        the wrong section and pass.
        """
        text = f"# Changelog\n\n{heading}\n\n## [0.12.0] - 2026-09-30\n"
        assert newest_released_version(text) is None, label

    def test_link_definitions_must_point_at_their_own_tag(self):
        own = "[0.12.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.12.0\n"
        crossed = "[0.12.0]: https://github.com/basecradle/basecradle-python/releases/tag/v0.13.0\n"
        assert link_versions(own) == ["0.12.0"]
        assert link_versions(crossed) == []


class TestChangelogAgreesWithTheVersion:
    """``CHANGELOG.md``'s three hand-edited copies of the version, pinned against the code.

    A release edits `_version.py`, the `## [X.Y.Z] - date` heading, and the
    `[X.Y.Z]: .../tag/vX.Y.Z` link definition. They have to agree, and until #229 nothing
    checked that they did -- a mistyped heading merged green and announced a version that
    was never built, on a PyPI filename that can never be replaced.
    """

    def test_every_section_heading_is_readable(self):
        """The anchor. Without it, every check here can pass while enforcing nothing.

        Both sides of the comparisons below are produced by these regexes, so a heading
        style they stop matching empties both sides and everything goes quietly green.
        This is the one check that looks at the raw shape of the file.
        """
        unreadable = [
            line
            for line in section_headings(CHANGELOG_TEXT)
            if line != _UNRELEASED_HEADING and re.fullmatch(_RELEASED_HEADING, line) is None
        ]
        assert not unreadable, (
            f"{CHANGELOG.name} has heading(s) this module cannot read: {unreadable}. "
            f"A released section must be exactly '## [X.Y.Z] - YYYY-MM-DD'."
        )

    def test_released_sections_are_newest_first(self):
        versions = released_versions(CHANGELOG_TEXT)
        descending = sorted(versions, key=as_tuple, reverse=True)
        assert versions == descending, (
            f"{CHANGELOG.name}'s released sections are out of order: {versions}. "
            f"A new section goes above the previous one, not below it."
        )

    def test_no_duplicate_released_sections(self):
        versions = released_versions(CHANGELOG_TEXT)
        duplicates = {v for v in versions if versions.count(v) > 1}
        assert not duplicates, f"{CHANGELOG.name} has two sections for: {sorted(duplicates)}"

    def test_every_released_section_has_a_link_definition(self):
        missing = set(released_versions(CHANGELOG_TEXT)) - set(link_versions(CHANGELOG_TEXT))
        assert not missing, (
            f"{CHANGELOG.name} has section(s) with no link definition: {sorted(missing)}. "
            f"Add '[X.Y.Z]: https://github.com/basecradle/basecradle-python/releases/tag/vX.Y.Z'."
        )

    def test_no_link_definition_without_a_released_section(self):
        """The other direction: a link to a tag no section announces is a 404 when clicked."""
        orphans = set(link_versions(CHANGELOG_TEXT)) - set(released_versions(CHANGELOG_TEXT))
        assert not orphans, (
            f"{CHANGELOG.name} links to release(s) it has no section for: {sorted(orphans)}"
        )

    def test_the_version_shipped_is_only_ever_final_or_a_dev_of_one(self):
        """#220 settled the policy: final releases publish, and main carries `X.Y.Z.dev0`.

        Stated here because everything below branches on it -- and because a version in
        any other shape (an `rc`, a `.post`) would otherwise reach the live assertions
        and fail on an unexplained mismatch rather than on the real problem.
        """
        assert IS_FINAL or IS_DEV, (
            f"__version__ is {basecradle.__version__!r}, which is neither a final X.Y.Z "
            f"nor an X.Y.Z.dev0. release.yml refuses to publish anything else (#220)."
        )

    @on_a_release
    def test_the_newest_section_is_the_version_being_shipped(self):
        assert newest_released_version(CHANGELOG_TEXT) == basecradle.__version__, (
            f"__version__ is {basecradle.__version__}, but {CHANGELOG.name}'s newest "
            f"section is {newest_released_version(CHANGELOG_TEXT)}. One of the two is a "
            f"typo, and whichever it is, the release would announce the wrong version."
        )

    @on_a_release
    def test_the_version_being_shipped_has_a_link_definition(self):
        assert basecradle.__version__ in link_versions(CHANGELOG_TEXT), (
            f"{CHANGELOG.name} has no link definition for {basecradle.__version__} "
            f"pointing at v{basecradle.__version__}."
        )

    @between_releases
    def test_a_dev_version_is_ahead_of_every_release(self):
        """The `.dev0` state carries a real invariant, so it is asserted rather than skipped.

        This is what catches a post-release bump typed *backwards* -- to a version already
        published -- which would otherwise sit unnoticed all cycle and then pass every
        guard at release time, surfacing only as PyPI's "File already exists" after the
        gate had been actuated.
        """
        newest = newest_released_version(CHANGELOG_TEXT)
        assert newest is not None
        assert as_tuple(BASE_VERSION) > as_tuple(newest), (
            f"__version__ is {basecradle.__version__}, but {newest} is already released. "
            f"The post-release bump must go forwards."
        )

    @between_releases
    def test_a_dev_version_has_no_changelog_section_yet(self):
        """Its section is written by the release PR, together with the bump to final."""
        assert BASE_VERSION not in released_versions(CHANGELOG_TEXT), (
            f"{CHANGELOG.name} already has a section for {BASE_VERSION}, but "
            f"__version__ is still {basecradle.__version__} -- the release has not shipped."
        )
