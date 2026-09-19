"""The workflow-hygiene invariant: nothing this repo's CI uploads outlives 30 days.

``constitution.md`` → How We Build: whatever a build leaves behind has a named owner, a
fixed home, and a stated end, and nothing outlives 30 days without a written reason. A
GitHub Actions ``upload-artifact`` step with no ``retention-days`` silently inherits the
90-day repository default — exactly the un-ended leaving the rule forbids, and the defect
fixed in #166. Nothing stopped it reappearing in the next workflow, so this test pins it
(CLAUDE.md → Conventions, "Tests pin invariants").

The scan is a deliberately narrow text match, not a YAML parse: a parser would mean a
dev-group dependency for four small hand-written workflow files, and "every dependency is
debt" (founder decision, #169). The matcher therefore errs toward *over*-matching —
anything shaped like an upload-artifact step must carry a literal, in-range
``retention-days``, and a step form this scanner cannot read is reported rather than
skipped.

A hand-rolled matcher's own blind spots are the real risk here: one that quietly stops
matching leaves this check green while it enforces nothing. Two things guard against that
— ``test_no_upload_step_escapes_the_scanner``, which flags any upload step written in a
shape the matcher cannot read, and ``TestTheScannerItself``, which pins where the matcher
fires and where it does not.

What it deliberately does not read, each failing loudly rather than passing: **flow style**
(``with: {retention-days: 30}``) and **anchors** — write workflow steps in block style, as
every workflow here does; and **reusable workflows**, whose steps live in the repo that
owns them, so an artifact uploaded by ``basecradle/.github`` is that repo's invariant to
keep, not this scan's to enforce.
"""

import re
from pathlib import Path
from typing import NamedTuple

import pytest

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"

#: The cap from the constitution. Longer needs a written reason, and this test to change.
MAX_RETENTION_DAYS = 30

#: A ``uses:`` pulling in any action whose reference contains "upload-artifact" —
#: ``actions/upload-artifact``, a SHA-pinned form, or a third-party fork. Loose on the
#: reference, and it matches whether ``uses:`` leads the step (``- uses:``) or follows a
#: ``name:``, so a new upload step is caught however it is written.
UPLOAD_ACTION = re.compile(r"^\s*(?:-\s+)?uses:\s*[\"']?(?P<action>\S*upload-artifact[^\s\"']*)")

#: ``\S+`` deliberately captures one bare token, so a trailing ``# comment`` is ignored
#: while a ``${{ … }}`` expression fails the checks below instead of sneaking by.
RETENTION_DAYS = re.compile(r"^\s*retention-days:\s*(?P<value>\S+)")


def _dash_column(line: str) -> int | None:
    """The column of this line's YAML list dash, or ``None`` if it starts no list item.

    A dash alone on its line starts an item just as ``- uses:`` does, and must be
    recognised as one: miss it and the item's body runs backwards into its predecessor.
    """
    stripped = line.lstrip()
    if stripped == "-" or stripped.startswith("- "):
        return len(line) - len(stripped)
    return None


def _is_structural(line: str) -> bool:
    """Whether this line can bound a step.

    Blank lines and comments carry no YAML structure: a comment is free to sit at any
    column, so letting one at a shallow indent read as a dedent would cut a step's body
    short and report a bounded step as unbounded.
    """
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _key_indent(line: str) -> int:
    """The column a YAML key sits at on this line, seeing through a ``- `` list dash."""
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    return indent + 2 if stripped.startswith("- ") else indent


class UploadStep(NamedTuple):
    """One upload-artifact step, with the workflow lines that make up its body."""

    workflow: Path
    line: int  # 1-indexed, the `uses:` line that pulls in the action
    action: str
    key_indent: int  # the column the step's own keys (`uses:`, `with:`) sit at
    body: list[str]

    def __str__(self) -> str:
        return f"{self.workflow.name}:{self.line} ({self.action})"


def _step_containing(lines: list[str], uses_index: int) -> list[str]:
    """Every line of the step owning ``lines[uses_index]``, that line included.

    A step runs from its own dash to the next sibling item, or to the first line that
    dedents out of the step list. Both boundaries are found by dash *column* rather than
    by the presence of a key, so a bare ``-`` bounds the step and a nested list inside a
    ``with:`` input (whose dashes sit deeper) does not. Getting either edge wrong is what
    makes the whole check lie: run the body backwards and a neighbour's ``retention-days``
    vouches for an unbounded step; cut it short and a bounded step reads as unbounded.
    """
    key_indent = _key_indent(lines[uses_index])

    start = uses_index
    for index in range(uses_index, -1, -1):
        dash = _dash_column(lines[index])
        if dash is not None and dash < key_indent:
            start = index
            break

    end = uses_index + 1
    while end < len(lines):
        line = lines[end]
        dash = _dash_column(line)
        if _is_structural(line) and (
            _key_indent(line) < key_indent or (dash is not None and dash < key_indent)
        ):
            break
        end += 1

    return lines[start:end]


def upload_artifact_steps(directory: Path = WORKFLOWS) -> list[UploadStep]:
    """Every upload-artifact step across the workflow files, in file order."""
    steps = []
    for workflow in sorted(directory.glob("*.y*ml")):
        lines = workflow.read_text().splitlines()
        for index, line in enumerate(lines):
            match = UPLOAD_ACTION.match(line)
            if match:
                steps.append(
                    UploadStep(
                        workflow=workflow,
                        line=index + 1,
                        action=match.group("action"),
                        key_indent=_key_indent(line),
                        body=_step_containing(lines, index),
                    )
                )
    return steps


def unscanned_upload_lines(directory: Path = WORKFLOWS) -> list[str]:
    """Every naming of the action, outside a comment, that ``UPLOAD_ACTION`` cannot read.

    The attribution runs over *mentions* rather than over ``uses:``-shaped lines, because
    the shapes worth catching are exactly the ones that do not look like a ``uses:`` line
    — a flow-style step, or a ``uses:`` whose value sits on the next line. Anything the
    matcher cannot account for is reported; nothing is skipped quietly.
    """
    return [
        f"{workflow.name}:{index}: {line.strip()}"
        for workflow in sorted(directory.glob("*.y*ml"))
        for index, line in enumerate(workflow.read_text().splitlines(), start=1)
        if "upload-artifact" in line.split("#", 1)[0] and not UPLOAD_ACTION.match(line)
    ]


def check_bounded_retention(step: UploadStep) -> None:
    """Assert the step declares exactly one literal ``retention-days`` within the cap."""
    # `retention-days` is an input of the action, so it must be nested under `with:` —
    # deeper than the step's own keys. At the step's key level Actions does not accept it,
    # and treating it as a cap would pass a workflow that never applies one.
    misplaced = [
        line.strip()
        for line in step.body
        if RETENTION_DAYS.match(line) and _key_indent(line) <= step.key_indent
    ]
    assert not misplaced, (
        f"{step}: {misplaced} sits at the step's own key level, but `retention-days` is a "
        f"`with:` input of the action — indent it under `with:`, or the artifact silently "
        f"keeps GitHub's 90-day default."
    )

    declared = [
        match
        for line in step.body
        if (match := RETENTION_DAYS.match(line)) and _key_indent(line) > step.key_indent
    ]

    assert len(declared) == 1, (
        f"{step}: expected exactly one `retention-days:`, found {len(declared)}. "
        f"Unset, the artifact inherits GitHub's 90-day default — a leaving with no "
        f"stated end (constitution.md → How We Build)."
    )

    # Quotes are legal YAML around a scalar and say nothing about the value.
    value = declared[0].group("value").strip("\"'")
    assert value.isdecimal(), (
        f"{step}: retention-days is {value!r}, not a plain base-10 integer this check "
        f"can verify. Keep it a literal number so the bound stays readable."
    )

    days = int(value)
    assert 1 <= days <= MAX_RETENTION_DAYS, (
        f"{step}: retention-days is {days}, outside 1–{MAX_RETENTION_DAYS}. "
        f"(0 means 'use the repository default' — the 90 days this test exists to "
        f"prevent. Longer than {MAX_RETENTION_DAYS} needs a written reason and a change "
        f"to MAX_RETENTION_DAYS.)"
    )


UPLOAD_STEPS = upload_artifact_steps()


def test_the_scanner_finds_the_upload_steps():
    """The scan matches something — otherwise the check below is a vacuous pass.

    If this ever fails because the last upload step was genuinely removed, retire this
    module with it.
    """
    assert WORKFLOWS.is_dir(), f"{WORKFLOWS} is missing"
    assert UPLOAD_STEPS, (
        f"no upload-artifact step matched under {WORKFLOWS} — either the last one was "
        f"removed, or a new step shape slipped past UPLOAD_ACTION and is now unchecked"
    )


def test_no_upload_step_escapes_the_scanner():
    """No upload step is written in a shape the matcher cannot read.

    The guard above is directory-wide, so one readable step keeps it green while an
    unreadable one beside it goes unchecked. This closes that gap: an upload step the
    matcher misses is a failure here, not a silent skip.
    """
    assert unscanned_upload_lines() == [], (
        "these lines pull in upload-artifact in a shape UPLOAD_ACTION cannot read, so "
        "their retention is unchecked — rewrite them as a plain `uses:` step, or widen "
        f"UPLOAD_ACTION to cover the new shape: {unscanned_upload_lines()}"
    )


@pytest.mark.parametrize("step", UPLOAD_STEPS, ids=str)
def test_upload_artifact_step_declares_bounded_retention(step: UploadStep):
    """Every artifact this repo uploads has a stated end, and it is at most 30 days."""
    check_bounded_retention(step)


class TestTheScannerItself:
    """Where the narrow text match does and does not fire, pinned against fabricated
    workflow fragments — the blind spots that would make the check above green but empty.
    """

    @pytest.fixture
    def workflow(self, tmp_path):
        """Write one fabricated workflow file and return its directory."""

        def _workflow(content: str) -> Path:
            (tmp_path / "fabricated.yml").write_text(content)
            return tmp_path

        return _workflow

    @pytest.fixture
    def scan(self, workflow):
        """Run the scanner over one fabricated workflow file."""
        return lambda content: upload_artifact_steps(workflow(content))

    @pytest.mark.parametrize(
        "uses_line",
        [
            "      - uses: actions/upload-artifact@v7",
            '      - uses: "actions/upload-artifact@v7"',
            "      - uses: actions/upload-artifact@d4f7e1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8",
            "      - uses: someorg/upload-artifact-fork@v1",
        ],
        ids=["tagged", "quoted", "sha-pinned", "third-party-fork"],
    )
    def test_finds_every_form_of_the_action_reference(self, scan, uses_line):
        """Loose on the reference: a fork or a SHA pin is still an upload step."""
        assert len(scan(f"jobs:\n  build:\n    steps:\n{uses_line}\n")) == 1

    def test_finds_a_step_whose_uses_follows_a_name(self, scan):
        """The under-match trap: matching only ``- uses:`` would miss this step entirely
        and silently leave it unchecked."""
        steps = scan(
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      - name: Upload the dist\n"
            "        uses: actions/upload-artifact@v7\n"
            "        with:\n"
            "          retention-days: 30\n"
        )
        assert len(steps) == 1
        check_bounded_retention(steps[0])

    def test_a_comment_naming_the_action_is_not_a_step(self, scan, workflow):
        """The over-match limit: prose about the action must not be scanned as a step,
        nor reported as an unreadable one."""
        content = (
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      # never add a bare `uses: actions/upload-artifact` with no cap\n"
            "      - uses: actions/checkout@v7\n"
        )
        assert scan(content) == []
        assert unscanned_upload_lines(workflow(content)) == []

    @pytest.mark.parametrize(
        "step_lines",
        [
            "      - {uses: actions/upload-artifact@v7}",
            "      - uses:\n          actions/upload-artifact@v7",
        ],
        ids=["flow-style", "value-on-the-next-line"],
    )
    def test_reports_a_step_shape_it_cannot_read(self, scan, workflow, step_lines):
        """Legal YAML the matcher does not read must be *reported*, not skipped — an
        unreadable step is how this check would go green while enforcing nothing.
        """
        content = f"jobs:\n  build:\n    steps:\n{step_lines}\n"
        assert scan(content) == []
        assert len(unscanned_upload_lines(workflow(content))) == 1

    def test_retention_days_must_be_an_input_not_a_step_key(self, scan):
        """A cap indented at the step's own key level is not a `with:` input, so Actions
        never applies it — counting it as bounded would pass an uncapped artifact."""
        (step,) = scan(
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      - uses: actions/upload-artifact@v7\n"
            "        retention-days: 30\n"
            "        with:\n"
            "          name: dist\n"
        )
        with pytest.raises(AssertionError, match="step's own key level"):
            check_bounded_retention(step)

    def test_a_dash_alone_on_its_line_still_bounds_the_step(self, scan):
        """The neighbour-vouching trap: if a bare ``-`` does not bound the step, this
        unbounded upload runs backwards into the checkout step and borrows its cap."""
        steps = scan(
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      - uses: actions/checkout@v7\n"
            "        with:\n"
            "          retention-days: 30\n"
            "      -\n"
            "        uses: actions/upload-artifact@v7\n"
            "        with:\n"
            "          name: dist\n"
        )
        assert len(steps) == 1
        with pytest.raises(AssertionError, match="found 0"):
            check_bounded_retention(steps[0])

    def test_a_neighbours_retention_does_not_vouch_for_an_unbounded_step(self, scan):
        """The block-boundary trap: a whole-file search would pass this workflow."""
        steps = scan(
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      - uses: actions/upload-artifact@v7\n"
            "        with:\n"
            "          name: dist\n"
            "      - uses: actions/upload-artifact@v7\n"
            "        with:\n"
            "          retention-days: 30\n"
        )
        assert len(steps) == 2
        with pytest.raises(AssertionError, match="found 0"):
            check_bounded_retention(steps[0])
        check_bounded_retention(steps[1])

    @pytest.mark.parametrize(
        ("body", "expected_error"),
        [
            ("          name: dist", "found 0"),
            ("          retention-days:", "found 0"),
            ("          retention-days: 0", "outside"),
            ("          retention-days: 90", "outside"),
            ("          retention-days: ${{ inputs.days }}", "not a plain base-10 integer"),
            ("          retention-days: 3²", "not a plain base-10 integer"),
        ],
        ids=["absent", "empty", "zero-means-default", "over-the-cap", "expression", "not-base-10"],
    )
    def test_rejects_an_unbounded_step(self, scan, body, expected_error):
        """Each way an artifact escapes a stated end fails — with an assertion naming the
        way it was, never an uncaught conversion error."""
        (step,) = scan(
            f"jobs:\n  build:\n    steps:\n"
            f"      - uses: actions/upload-artifact@v7\n        with:\n{body}\n"
        )
        with pytest.raises(AssertionError, match=expected_error):
            check_bounded_retention(step)

    @pytest.mark.parametrize(
        "body",
        [
            "          retention-days: 30",
            "          retention-days: 1",
            '          retention-days: "30"',
            "          retention-days: 30  # the constitutional cap",
            "          name: dist\n\n          retention-days: 14",
            "          tags:\n            - alpha\n            - beta\n          retention-days: 7",
            "          name: dist\n# a note at column zero\n          retention-days: 21",
        ],
        ids=[
            "at-the-cap",
            "minimum",
            "quoted",
            "trailing-comment",
            "blank-line-inside-the-step",
            "list-valued-input-before-it",
            "shallow-comment-inside-the-step",
        ],
    )
    def test_accepts_a_bounded_step(self, scan, body):
        """The legitimate spellings stay green — a false alarm here would block a
        correctly-bounded workflow edit."""
        (step,) = scan(
            f"jobs:\n  build:\n    steps:\n"
            f"      - uses: actions/upload-artifact@v7\n        with:\n{body}\n"
        )
        check_bounded_retention(step)
