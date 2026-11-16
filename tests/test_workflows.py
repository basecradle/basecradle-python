"""Seven workflow invariants: bounded artifact retention, a gate that covers every job, a
gate that cannot fail open, a release pipeline that publishes what it says it does, a test
matrix that runs exactly the Python versions the package declares, with a
``requires-python`` floor at the oldest of them (#336, #338), an action
pinned to a commit SHA in every workflow that holds a secret or a write grant (#339), and a
build toolchain held by uv.lock rather than fetched fresh on each tag (#341, #346). The last three
carry their reasons beside their code, at the end of this file.

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

The second invariant (#238): every job in ``ci.yml`` is in the ``ci`` gate's ``needs``.
The gate is the single required status check in branch protection, and its coverage is a
hand-maintained list — a job added without editing that line runs on every PR, goes red,
and **merges anyway**, because it is not the required check. That failure looks exactly
like working CI. The list went from four entries to six in one session (#226, #236) on
memory alone.

It is read by text matching for the same reason as the scan above, and it inherits the
same discipline: ``unreadable_job_lines`` reports a job key the matcher cannot read rather
than dropping it, because a dropped job key is precisely the bug this is here to catch.

The third (#243) is the rest of that gate's fail-open surface. #238 pinned one way the
gate fails open; three more one-line edits reach the same outcome -- red CI that merges
green -- and every one of them reads as tidying. Drop the gate's own ``if: always()`` and
GitHub *skips* the gate the moment a dependency fails, which branch protection counts as
passing. Narrow the gate step's condition to ``failure`` alone, or give that step a
``continue-on-error``, and a skipped dependency leaves the gate green. Set
``continue-on-error`` at **job** level on a dependency and it reports ``success`` into
``needs`` even when it failed. Nothing in this repo read those lines; now something does.

The fourth (#243) is ``release.yml``. PyPI's Trusted Publisher is registered against a
workflow **filename** and an **environment name** (CLAUDE.md -> Releasing, "Contractual
names"), and neither was written anywhere this repo checks. Renaming either is a publish
that 403s on a tag already pushed, under a version number PyPI never lets go of. The
rehearsal ordering -- the real publish waiting on the TestPyPI one -- is pinned beside
them: lose it and the rehearsal runs in parallel with the thing it exists to rehearse.

#250 finished both lists. #243's enumeration of the gate's fail-open edits had one more
member nobody had named: the step's ``run: exit 1``. Change it to ``exit 0`` and the
condition still fires, the step still runs, and the gate reports success on every red
dependency -- past all four of #243's checks, because none of them read the command. And
three more contractual facts of ``release.yml`` had no pin: ``permissions: id-token:
write``, without which no OIDC token is minted and Trusted Publishing cannot authenticate
at all; the rehearsal's ``repository-url``, whose absence makes ``publish-testpypi``
upload to **pypi.org** -- under the ``testpypi`` environment, which carries no protection
rules, so the release ships before the ``pypi`` gate is ever actuated; and job-level
``continue-on-error``, which would let a failed rehearsal satisfy the very ``needs`` edge
#243 pinned. Each of those is one line, and each surfaces at the one moment nothing can
be undone.
"""

import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple

import pytest

from scripts.locked_requirement import locked_package

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


def _strip_comment(value: str) -> str:
    """``value`` without a trailing YAML comment — a ``#`` that opens one.

    Two of YAML's rules about ``#``, because reading a value differently from the way
    Actions reads it is the quiet kind of wrong. A ``#`` opens a comment only when it
    follows whitespace — in ``a#b`` it belongs to the scalar — and never inside a quoted
    scalar, so ``"CI # 1"`` is that whole name and not ``CI``.

    The start-anchored half matters because ``KEY_LINE`` has already eaten the whitespace
    ahead of the value, so a key whose only content is a comment
    (``environment:  # actuated by the capital``) arrives here as ``#…``. Read as a value
    it would be a phantom environment name, or a phantom job in a ``needs:``.
    """
    if value.startswith(("'", '"')):
        closing = value.find(value[0], 1)
        if closing != -1:
            return value[: closing + 1].strip()
    return re.split(r"(?:^|\s+)#", value, maxsplit=1)[0].strip()


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


# ---------------------------------------------------------------------------------------
# #238: the gate must depend on every job
# ---------------------------------------------------------------------------------------

#: The two workflows with checks of their own below. Declared together and up here
#: because the blind-spot scans run over both, and a parametrize over them is evaluated
#: at import — before either section that owns them.
CI_WORKFLOW = WORKFLOWS / "ci.yml"
RELEASE_WORKFLOW = WORKFLOWS / "release.yml"

#: The gate's job key, and the check name branch protection requires. Both matter: the
#: key is what ``needs`` entries and this scan refer to, while the **name** is what GitHub
#: reports and what branch protection matches on -- so renaming only the name would leave
#: the required check never arriving, with every job still listed correctly.
GATE_JOB = "ci"
GATE_CHECK_NAME = "CI"

#: A job key: exactly two spaces, a key, nothing else. Meaningful only inside the
#: top-level ``jobs:`` block -- ``on:`` and ``concurrency:`` carry two-space keys too
#: (``pull_request:``, ``group:``).
JOB_KEY = re.compile(r"^ {2}(?P<name>[A-Za-z_][\w-]*):\s*$")

#: Anything else at two-space indent inside the jobs block: a quoted key, a key with a
#: trailing comment, a key with a value. Reported rather than skipped.
JOB_KEY_SHAPED = re.compile(r"^ {2}\S")

#: The column a job's OWN keys sit at — ``name:``, ``needs:``, ``if:``, ``environment:``.
#: One fact, and the whole correctness argument of the readers below rests on it: a key
#: at this column belongs to the job, and the same key deeper belongs to one of its steps.
JOB_LEVEL = 4

#: Any ``key: value`` line, seeing through a leading list dash, with the value empty when
#: it is a nested block. Useless on its own and deliberately so: it is only ever paired
#: with ``_key_indent``, because the COLUMN is what separates a job's own ``name:``/``if:``
#: from a step's key of the same name, and reading one as the other both false-passes and
#: false-fails.
KEY_LINE = re.compile(r"^\s*(?:-\s+)?(?P<key>[A-Za-z_][\w.-]*):(?:\s+(?P<value>.*?))?\s*$")


def _jobs_block(text: str) -> list[str]:
    """The lines inside the top-level ``jobs:`` block.

    Indentation alone is enough to read this safely, and deliberately so -- no tracking of
    block scalars. A ``run: |`` body is arbitrary text, but YAML requires its content to
    be *more* indented than its key, and inside ``jobs:`` that key is already at six
    spaces. So scalar content can never sit at the two spaces ``JOB_KEY`` reads, nor at
    the column zero that closes the block: a column-0 line ends the scalar in YAML itself,
    which is why the obvious counter-example (a heredoc writing ``name:`` at column 0)
    does not parse at all -- GitHub and ``actionlint`` both reject it before this test
    runs.
    """
    lines: list[str] = []
    in_jobs = False

    for line in text.splitlines():
        if not in_jobs:
            in_jobs = line.rstrip() == "jobs:"
            continue
        if line.strip() and not line.startswith((" ", "#")):
            break  # a new top-level key closes the block
        lines.append(line)

    return lines


def ci_job_names(text: str) -> list[str]:
    """Every readable job key in a workflow, in the order written."""
    return [m.group("name") for line in _jobs_block(text) if (m := JOB_KEY.match(line))]


def unreadable_job_lines(text: str) -> list[str]:
    """Two-space lines inside ``jobs:`` that are key-shaped but that JOB_KEY cannot read.

    The blind-spot guard, and the important half of this pair. A job key written
    ``  docs:  # new`` or ``  "docs":`` would otherwise never enter the comparison below,
    so the job would be un-gated and the check would pass -- the exact failure it exists
    to catch.
    """
    return [
        line.rstrip()
        for line in _jobs_block(text)
        if JOB_KEY_SHAPED.match(line)
        and not JOB_KEY.match(line)
        and not line.lstrip().startswith("#")
    ]


def job_bodies(text: str) -> dict[str, list[str]]:
    """Every readable job key mapped to the lines of that job's body.

    Bodies are bounded by the two-space key lines themselves — *including* the ones
    ``JOB_KEY`` cannot read. A body is only ever attributed to a key this scanner read, so
    an unreadable key never donates its keys to the job above it: that silent merge is the
    same class of bug ``unreadable_job_lines`` exists to refuse.

    Scoping every later reader to one job's body is what keeps them honest — an inline
    ``needs:`` on an earlier job must not read as the gate's, which would both false-pass
    (an earlier list that happens to match) and false-fail (blaming the gate for a list it
    never wrote).
    """
    bodies: dict[str, list[str]] = {}
    body: list[str] | None = None

    for line in _jobs_block(text):
        if JOB_KEY_SHAPED.match(line) and not line.lstrip().startswith("#"):
            match = JOB_KEY.match(line)
            # Assigned, never appended to: a repeated job key is invalid YAML that
            # actionlint rejects before this test runs, but splicing two blocks into one
            # body would let either donate keys to the other — the neighbour-vouching
            # merge this function exists to refuse. Last wins, as GitHub's parser reads it.
            body = None
            if match:
                body = bodies[match.group("name")] = []
        elif body is not None:
            body.append(line)

    return bodies


def level_key_value(lines: list[str], key: str, level: int) -> str | None:
    """The value of ``key:`` written at exactly ``level``, comment stripped, or ``None``.

    The column is exact, not a bound, which is what makes it safe: at ``JOB_LEVEL`` the
    reason ``_jobs_block`` gives applies — a ``run: |`` body inside a job is always deeper
    than the six-space step key owning it, so block-scalar content never reaches it — and
    at column 0 the same holds for the whole file.

    ``None`` when the key is absent; ``""`` when it is present with the value on the lines
    beneath, which is a mapping, a sequence or a block scalar for someone else to read.
    """
    for line in lines:
        match = KEY_LINE.match(line)
        if match and match.group("key") == key and _key_indent(line) == level:
            return _strip_comment(match.group("value") or "")
    return None


def job_key_value(body: list[str], key: str) -> str | None:
    """A job's own ``key:`` value — the job-level reading of ``level_key_value``."""
    return level_key_value(body, key, JOB_LEVEL)


def _parse_needs_value(value: str) -> list[str] | None:
    """A ``needs:`` value as job keys, or ``None`` when the matcher cannot read it.

    Both one-line spellings, because both are legal and either could be written: the
    inline array (``ci``'s ``needs: [lint, test]``) and the single-job scalar
    (``publish-pypi``'s ``needs: publish-testpypi``). Quotes are legal YAML around a
    scalar and say nothing about the value.

    A block list — ``needs:`` with ``- lint`` beneath it — reads as ``None``, never as
    ``[]``: "depends on nothing" and "this matcher cannot read the line" are different
    failures, and only one of them is the workflow's fault.
    """
    if value.startswith("[") and value.endswith("]"):
        return [job.strip().strip("\"'") for job in value[1:-1].split(",") if job.strip()]
    # A bare scalar holds none of YAML's flow punctuation and is not a block-scalar
    # header; anything else is a shape this matcher has not been taught, and saying so
    # beats handing back a dependency on a job literally named ">-".
    if _is_unread(value) or any(character in value for character in "[]{},"):
        return None
    return [value.strip("\"'")]


def job_needs(body: list[str]) -> list[str] | None:
    """The jobs this job waits on, or ``None`` when its ``needs:`` is absent or unreadable."""
    value = job_key_value(body, "needs")
    return None if value is None else _parse_needs_value(value)


def gate_body(text: str) -> list[str]:
    """The gate job's body lines, or an empty list when the workflow declares no gate.

    One place that knows which job the gate is, so every check below reads as a statement
    about the gate rather than as another repetition of how to find it.
    """
    return job_bodies(text).get(GATE_JOB, [])


def gate_needs(text: str) -> list[str] | None:
    """The gate job's ``needs`` entries, or ``None`` if its ``needs:`` is unreadable."""
    return job_needs(gate_body(text))


def gate_check_name(text: str) -> str | None:
    """The gate job's ``name:`` — what GitHub reports and branch protection matches."""
    name = job_key_value(gate_body(text), "name")
    return None if name is None else name.strip("\"'")


def test_the_gate_scanner_read_the_workflow():
    """The extraction found something — otherwise the check below is a vacuous pass."""
    assert CI_WORKFLOW.is_file(), f"{CI_WORKFLOW} is missing"
    jobs = ci_job_names(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert len(jobs) > 1, (
        f"read {jobs} as the jobs in {CI_WORKFLOW.name} — either the file was restructured "
        f"or JOB_KEY stopped matching, and the gate check below is empty"
    )
    assert GATE_JOB in jobs, f"no {GATE_JOB!r} job in {CI_WORKFLOW.name}"


def test_no_job_key_escapes_the_scanner():
    """No job is written in a shape JOB_KEY cannot read.

    Without this, a job key the matcher misses is simply absent from the comparison
    below — so the job is un-gated *and* the check is green, which is the failure this
    whole section exists to prevent.
    """
    unreadable = unreadable_job_lines(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert unreadable == [], (
        f"these lines in {CI_WORKFLOW.name}'s jobs block are key-shaped but unreadable to "
        f"JOB_KEY, so any job among them is unchecked — write the key plainly as "
        f"`  name:`, or widen JOB_KEY: {unreadable}"
    )


def test_the_gate_needs_line_is_readable():
    """An unreadable ``needs:`` is its own failure, not "the gate depends on nothing"."""
    assert gate_needs(CI_WORKFLOW.read_text(encoding="utf-8")) is not None, (
        f"could not read the {GATE_JOB!r} job's `needs:` in {CI_WORKFLOW.name}. If it was "
        f"rewritten as a block list, teach _parse_needs_value that shape — do not leave "
        f"it unread"
    )


def test_the_gate_depends_on_every_job():
    """Every job in ``ci.yml`` is in the ``ci`` gate's ``needs``, and nothing else is.

    Set equality, not containment, because both directions are bugs: a job missing from
    ``needs`` is not required and merges red, and a ``needs`` entry naming a job that no
    longer exists makes GitHub fail the whole workflow.

    The other half — the gate treating a *skipped* dependency as a failure, since GitHub
    counts skipped required checks as passing — was deliberately left as a comment here by
    the capital's decision on #238, and is now asserted in its own right by
    ``test_the_gate_step_fails_on_every_bad_result`` (#243).
    """
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    jobs = set(ci_job_names(text)) - {GATE_JOB}
    needs = gate_needs(text)
    assert needs is not None, "unreadable needs line; see test_the_gate_needs_line_is_readable"
    assert jobs == set(needs), (
        f"the {GATE_JOB!r} gate's `needs` must name every other job in "
        f"{CI_WORKFLOW.name}. jobs={sorted(jobs)} needs={sorted(set(needs))}. A job "
        f"missing from `needs` is not part of the required check: it runs, goes red, and "
        f"merges anyway."
    )
    assert len(needs) == len(set(needs)), f"duplicate entries in the gate's `needs`: {needs}"


def test_the_gate_reports_under_the_name_branch_protection_requires():
    """Renaming the job's ``name:`` un-gates the repo while every ``needs`` stays correct.

    Branch protection matches the *reported check name*, not the job key. So a rename here
    means the required check never arrives and every PR is mergeable with no CI, with this
    section's other tests all green.
    """
    name = gate_check_name(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert name == GATE_CHECK_NAME, (
        f"the {GATE_JOB!r} job reports as {name!r}, but branch protection requires "
        f"{GATE_CHECK_NAME!r}. Rename it back, or update branch protection *and* this test "
        f"together — a mismatch means no required check arrives at all."
    )


class TestTheGateScannerItself:
    """Where the job/needs extraction fires and where it does not, on fabricated files.

    Every case here is a blind spot that would leave the checks above green and empty.
    """

    JOBS = "jobs:\n  lint:\n    runs-on: x\n"

    def test_two_space_keys_outside_the_jobs_block_are_not_jobs(self):
        text = (
            "name: CI\non:\n  pull_request:\n  push:\n    branches: [main]\n"
            "concurrency:\n  group: x\n" + self.JOBS
        )
        assert ci_job_names(text) == ["lint"]

    def test_a_top_level_key_after_jobs_closes_the_block(self):
        assert ci_job_names(self.JOBS + "something-else:\n  not-a-job:\n") == ["lint"]

    def test_yaml_inside_a_run_block_is_not_read_as_structure(self):
        """A script that writes YAML must not be mistaken for jobs.

        Indentation is what makes this safe: the scalar's content is deeper than the
        job keys, so nothing in it sits at the two spaces JOB_KEY reads. The column-0
        variant of this would end the scalar in YAML itself and fail to parse, so it
        cannot reach this scanner.
        """
        text = (
            "jobs:\n"
            "  lint:\n"
            "    steps:\n"
            "      - run: |\n"
            "          cat <<'YML' > out.yml\n"
            "          jobs:\n"
            "            fake:\n"
            "          YML\n"
            "  docs:\n"
            "    runs-on: x\n"
        )
        assert ci_job_names(text) == ["lint", "docs"]
        assert unreadable_job_lines(text) == []

    @pytest.mark.parametrize(
        "line",
        ['  "docs":', "  docs:  # the new docs job", "  docs: {}"],
    )
    def test_a_job_key_it_cannot_read_is_reported_not_dropped(self, line):
        text = f"jobs:\n{line}\n    runs-on: x\n"
        assert ci_job_names(text) == []
        assert unreadable_job_lines(text) == [line]

    def test_a_comment_inside_the_jobs_block_is_not_an_unreadable_key(self):
        text = "jobs:\n  # a note about the jobs below\n  lint:\n    runs-on: x\n"
        assert ci_job_names(text) == ["lint"]
        assert unreadable_job_lines(text) == []

    def test_needs_is_read_from_the_gate_and_not_an_earlier_job(self):
        """An earlier job's inline `needs:` must not be mistaken for the gate's."""
        text = (
            "jobs:\n"
            "  lint:\n    runs-on: x\n"
            "  types:\n    needs: [lint]\n    runs-on: x\n"
            "  ci:\n    needs: [lint, types]\n    runs-on: x\n"
        )
        assert gate_needs(text) == ["lint", "types"]

    def test_a_gate_with_no_needs_reads_as_unreadable_not_empty(self):
        text = "jobs:\n  lint:\n    needs: [x]\n    runs-on: x\n  ci:\n    runs-on: x\n"
        assert gate_needs(text) is None

    def test_quoted_needs_entries_are_the_same_jobs(self):
        assert gate_needs("jobs:\n  ci:\n    needs: [\"lint\", 'test']\n") == ["lint", "test"]

    def test_a_trailing_comment_on_needs_is_still_readable(self):
        text = "jobs:\n  ci:\n    needs: [lint, test]  # every other job\n"
        assert gate_needs(text) == ["lint", "test"]

    def test_a_block_list_needs_is_reported_not_read_as_empty(self):
        assert gate_needs("jobs:\n  ci:\n    needs:\n      - lint\n") is None

    def test_an_empty_inline_list_reads_as_empty_not_unreadable(self):
        assert gate_needs("jobs:\n  ci:\n    needs: []\n") == []

    def test_reads_the_gates_reported_name(self):
        text = "jobs:\n  lint:\n    name: lint\n  ci:\n    name: CI\n    runs-on: x\n"
        assert gate_check_name(text) == "CI"

    def test_the_gates_name_is_not_taken_from_another_job(self):
        text = "jobs:\n  lint:\n    name: CI\n  ci:\n    runs-on: x\n"
        assert gate_check_name(text) is None


# ---------------------------------------------------------------------------------------
# #243: the rest of the gate's fail-open surface
# ---------------------------------------------------------------------------------------

#: A YAML block-scalar header standing where a value was expected: ``|``, ``>-``,
#: ``|2-``, ``>-2``. Both orderings of the chomping and indentation indicators, because
#: YAML permits either and a reader that knew only one would misdiagnose the other.
BLOCK_SCALAR = re.compile(r"^[|>][\d+-]*$")

#: The gate's own ``if:``. Without it GitHub SKIPS the gate the moment a dependency fails,
#: and branch protection counts a skipped required check as PASSING — so deleting the one
#: line that looks like a redundant always-true condition is the edit that merges red CI.
GATE_IF = "always()"

#: Every ``needs.*.result`` the gate's step must treat as a failure. ``skipped`` is the
#: load-bearing one, for the same reason ``always()`` is: a dependency that skips leaves
#: the gate's step unrun, the gate green, and the required check satisfied by a CI run
#: that never happened.
GATE_FAILING_RESULTS = ("failure", "cancelled", "skipped")

#: The gate step's condition in full, derived from the results above rather than typed
#: beside them. Naming the three results is not enough on its own: ``||`` swapped for
#: ``&&`` still names all three and fires only when all three happen at once, and a
#: leading ``!`` inverts it outright. Both are one-character edits that read as tidying,
#: so the expression is pinned whole. Reordering the clauses or changing the quotes fails
#: here too — deliberately: the failure is loud, explains itself, and this line is not one
#: to restyle casually.
GATE_STEP_IF = " || ".join(f"contains(needs.*.result, '{r}')" for r in GATE_FAILING_RESULTS)

#: What the gate's step runs when that condition fires. Another one-line edit of the
#: same family, and the one #243's enumeration missed: ``exit 0`` leaves the condition
#: firing, the step running, and the gate reporting success on every red dependency
#: (#250). Nothing above reads the command, so nothing above notices.
GATE_STEP_RUN = "exit 1"


def _is_unread(value: str) -> bool:
    """Whether what this matcher extracted is not the value at all.

    Two legal shapes put the content on the *following* lines and leave nothing useful on
    this one: a block-scalar header, and a plain multi-line scalar (``if:`` with the
    expression beneath it), which arrives here as the empty string. This reader follows
    neither, so both are reported as unread rather than diagnosed as a value that says
    the wrong thing — the distinction every reader in this module keeps.
    """
    return not value or bool(BLOCK_SCALAR.match(value))


def _normalize_scalar(value: str) -> str:
    """A scalar with surrounding quotes dropped and internal whitespace collapsed.

    Quotes are legal YAML around a scalar and say nothing about the value, and
    ``exit  1`` is the same command as ``exit 1`` — so a check comparing one of these
    exactly has to normalize first, or it reddens CI with a message untrue of the edit.
    One copy, shared by the gate's condition and the gate's command, so the two cannot
    come to disagree about what a quoted scalar is.
    """
    return " ".join(value.strip().strip("\"'").split())


def _unwrap_expression(condition: str) -> str:
    """A workflow condition without its optional ``${{ … }}`` wrapper, whitespace normal.

    An ``if:`` may be written either way and GitHub reads them identically; ``${{ … }}``
    is the form its own documentation uses. A reader that knew only one spelling would
    call the other a disabled gate — a red CI, with an error message that is simply untrue
    of the edit in front of it.
    """
    stripped = _normalize_scalar(condition)
    if stripped.startswith("${{") and stripped.endswith("}}"):
        stripped = stripped[3:-2]
    return " ".join(stripped.split())


def block_scalar_line_numbers(lines: list[str]) -> set[int]:
    """The lines of a job body that are YAML structure, not block-scalar content.

    A ``run: |`` body is arbitrary text at an arbitrary depth, and the readers below are
    depth-*bounded* rather than column-exact — so a shell heredoc that emits
    ``key: value`` lines is otherwise read as keys. ``_jobs_block`` can dismiss this at
    the job level because job keys sit at a fixed column; nothing deeper can.

    The scalar runs from its ``|``/``>`` header to the first structural line no deeper
    than that header, which is YAML's own rule. Blank lines inside it are content, not a
    dedent — treating one as a dedent would end the scalar early and put the rest of a
    script back into the reading.

    Returns **indices**, not lines, so a caller that reports file positions keeps them.
    Both the readers below and the blind-spot guards use it, which is the point: a guard
    that disagreed with its reader about what a shell script is would redden CI over an
    ``echo`` that happens to mention the key.
    """
    inside: set[int] = set()
    scalar_column: int | None = None

    for index, line in enumerate(lines):
        if scalar_column is not None:
            if not _is_structural(line) or _key_indent(line) > scalar_column:
                inside.add(index)
                continue  # still inside the scalar
            scalar_column = None

        match = KEY_LINE.match(line)
        if match and BLOCK_SCALAR.match(_strip_comment(match.group("value") or "")):
            scalar_column = _key_indent(line)

    return inside


def outside_block_scalars(lines: list[str]) -> list[str]:
    """``lines`` with every block-scalar body dropped — YAML structure only."""
    inside = block_scalar_line_numbers(lines)
    return [line for index, line in enumerate(lines) if index not in inside]


def step_values(lines: list[str], key: str) -> list[str]:
    """Every ``key:`` value below the job's own level, in order, comment stripped.

    Takes a job's body or a single step's lines — ``publish_repository_url`` passes one
    step, because attributing a ``with:`` input to the step that declares it is the whole
    point there.

    Deeper than ``JOB_LEVEL``, so a job's own key of the same name is never among them:
    the gate's ``if: always()`` and its step's ``if: contains(…)`` are separate fail-open
    surfaces — one decides whether the gate *runs*, the other whether it *fails* — and
    conflating them would let either vouch for the other.

    Deep enough to reach a ``with:`` input as well as a step's own key, which is what
    ``repository-url`` is. Being a depth *bound* rather than an exact column, it would
    also read straight into a ``run: |`` body — ``job_key_value``'s "block-scalar content
    never reaches this column" argument does not transfer, because that one pins
    ``JOB_LEVEL`` exactly and this cannot. Hence ``outside_block_scalars``: the scalars
    are dropped before the match, so a shell heredoc emitting ``key: value`` lines is not
    read as keys.
    """
    return [
        _strip_comment(match.group("value") or "")
        for line in outside_block_scalars(lines)
        if (match := KEY_LINE.match(line))
        and match.group("key") == key
        and _key_indent(line) > JOB_LEVEL
    ]


def continue_on_error(body: list[str]) -> list[tuple[int, str]]:
    """Every readable ``continue-on-error:`` in a job body, as ``(key column, value)``.

    The column carries the whole meaning. At ``JOB_LEVEL`` it is the **job's**, and a job
    that continues on error reports ``success`` into every ``needs`` naming it, however
    red it ran. Deeper it is a **step's**, which is legitimate and in use here —
    ``actionlint``'s advisory pass over the capital's stubs is exactly that.

    *Readable* means the value is on the line. One that is not — a block scalar, flow
    style — is left out rather than reported as an empty value, because an empty value
    compares as "not false" and the caller would accuse a
    ``continue-on-error:``/``false`` pair of setting the very switch it clears. Those
    shapes belong to ``unreadable_continue_on_error_lines``, which fails on them by name.

    The value keeps its quotes, which ``_is_enabled`` needs: quoted, ``false`` is not the
    boolean but a truthy string.
    """
    return [
        (_key_indent(line), value)
        for line in outside_block_scalars(body)
        if (match := KEY_LINE.match(line))
        and match.group("key") == "continue-on-error"
        and (value := _strip_comment(match.group("value") or ""))
    ]


def _is_enabled(value: str) -> bool:
    """Whether a ``continue-on-error`` value switches the thing on.

    Case-folded, because YAML 1.2's core schema reads ``false``, ``False`` and ``FALSE``
    as the same boolean and Actions honours all three — reporting one of them as fail-open
    would be a red CI over a workflow that sets nothing.

    *Unquoted* only, which is why the value arrives here with its quotes on. ``"false"``
    is the string, not the boolean, and a non-empty string is truthy to GitHub's
    expression evaluator — so the quoted spelling switches the thing **on**, and is
    exactly the near-miss worth failing on. Everything else counts as enabled too,
    expressions included: a ``${{ … }}`` this test cannot evaluate is a switch in an
    unknown position, and unknown is the direction to fail in.
    """
    return value.lower() != "false"


def unreadable_key_lines(workflow: Path, *keys: str) -> list[str]:
    """Every naming of these keys, outside a comment, that ``KEY_LINE`` cannot read.

    This module's blind-spot idiom, written once rather than once per key: a matcher that
    quietly stops matching leaves its check green while enforcing nothing, so a shape the
    matcher misses is *reported*, never skipped.

    The readability test is ``_strip_comment``, character for character the reading the
    matchers themselves take. Tested on the raw value instead,
    ``continue-on-error:  # while we debug`` looks readable here and reads as empty
    there; stripped differently, a value made only of quotes does the same. Either way
    the key escapes both halves of the pair at once, which is the one outcome this
    exists to deny.
    """
    assert keys, "a scan for no keys reports nothing and reads as a clean guard"

    lines = workflow.read_text(encoding="utf-8").splitlines()
    inside_a_script = block_scalar_line_numbers(lines)

    unreadable = []
    for index, line in enumerate(lines):
        # A `run: |` body is shell, not YAML, and the readers skip it -- so a guard that
        # did not would redden CI over an `echo` mentioning the key, with advice ("write
        # it as a plain key") that is untrue of the line in front of it.
        if index in inside_a_script:
            continue
        # `key:` rather than `key`, so a step named "never override repository-url here"
        # is prose about the input and not an unreadable declaration of it. The shapes
        # worth catching all still carry the colon: a flow mapping, and a key whose value
        # is on the next line.
        if not any(f"{key}:" in line.split("#", 1)[0] for key in keys):
            continue
        match = KEY_LINE.match(line)
        if match and match.group("key") in keys and _strip_comment(match.group("value") or ""):
            continue
        unreadable.append(f"{workflow.name}:{index + 1}: {line.strip()}")
    return unreadable


def unreadable_continue_on_error_lines(workflow: Path) -> list[str]:
    """Every ``continue-on-error`` in a workflow the matcher cannot read.

    Its value on the next line, or flow style, is invisible to ``continue_on_error`` — so
    the switch would be set, the checks below green, and nothing would say so.
    """
    return unreadable_key_lines(workflow, "continue-on-error")


def check_the_gate_runs_whatever_its_dependencies_did(text: str) -> None:
    """Assert the gate job still runs when a dependency did not succeed."""
    condition = job_key_value(gate_body(text), "if")
    assert condition is None or not _is_unread(condition), (
        f"the {GATE_JOB!r} job's `if:` reads {condition!r} — a block scalar, or a value "
        f"carried on the lines beneath. This reader does not follow it there, so the "
        f"gate's condition is unread, not wrong. Write it on one line, or teach "
        f"job_key_value that shape; an unread condition is how this check goes green "
        f"and empty."
    )
    assert condition is not None and _unwrap_expression(condition) == GATE_IF, (
        f"the {GATE_JOB!r} job's own `if:` reads {condition!r}, not {GATE_IF!r}. Without "
        f"`if: {GATE_IF}` GitHub skips the gate as soon as a dependency fails, and branch "
        f"protection counts a skipped required check as passing — the gate stops being a "
        f"gate and every red PR becomes mergeable."
    )


def check_the_gate_step_fails_on_every_bad_result(text: str) -> None:
    """Assert the gate's step fires on each ``needs.*.result`` that is not a success."""
    conditions = step_values(gate_body(text), "if")
    assert len(conditions) == 1, (
        f"expected exactly one conditional step in the {GATE_JOB!r} job, found "
        f"{len(conditions)}: {conditions}. The gate is one step on purpose; a second "
        f"condition means deciding which one is the gate, so make that decision here."
    )

    assert not _is_unread(conditions[0]), (
        f"the {GATE_JOB!r} gate's step `if:` reads {conditions[0]!r} — a block scalar, or "
        f"a value carried on the lines beneath. This reader does not follow it there, so "
        f"the condition is unread, not wrong. Write it on one line, or teach "
        f"step_values that shape."
    )

    condition = _unwrap_expression(conditions[0])
    missing = [result for result in GATE_FAILING_RESULTS if result not in condition]
    assert not missing, (
        f"the {GATE_JOB!r} gate's step condition does not name {missing}: {condition!r}. "
        f"A result it does not name leaves the step unrun and the gate green. "
        f"'skipped' especially: GitHub reports a skipped required check as passing, so "
        f"dropping it is the whole bypass in one word."
    )

    # Naming the three is necessary and not sufficient: `&&` for `||` names all three and
    # fires only when all three happen at once, and a leading `!` inverts the lot. Neither
    # drops a word, so only the whole expression catches them.
    assert condition == GATE_STEP_IF, (
        f"the {GATE_JOB!r} gate's step condition reads\n  {condition}\nbut must be\n  "
        f"{GATE_STEP_IF}\nIt names every result, so the operators are what changed: `&&` "
        f"fires only when all three happened at once, and a leading `!` inverts the gate "
        f"outright. If this is a deliberate rewrite, change it here in the same PR."
    )


def check_the_gate_step_actually_fails(text: str) -> None:
    """Assert the gate's step still exits non-zero when its condition fires.

    Every other check in this section reads *whether* the step runs. None reads what it
    then does — so ``run: exit 0``, or a ``run: echo ...``, passes all of them while the
    gate reports success on every red dependency.
    """
    runs = step_values(gate_body(text), "run")
    assert len(runs) == 1, (
        f"expected exactly one step-level `run:` in the {GATE_JOB!r} job, found "
        f"{len(runs)}: {runs}. The gate is one step on purpose; a second command means "
        f"deciding which one fails the build, so make that decision here."
    )
    assert not _is_unread(runs[0]), (
        f"the {GATE_JOB!r} gate's step `run:` reads {runs[0]!r} — a block scalar, or a "
        f"value carried on the lines beneath. This reader does not follow it there, so "
        f"the gate's command is unread, not wrong."
    )
    # Quotes are legal YAML around a scalar and say nothing about the value; every other
    # reader here tolerates them, and reddening CI over `run: "exit 1"` would be an error
    # message untrue of the edit in front of it.
    assert _normalize_scalar(runs[0]) == GATE_STEP_RUN, (
        f"the {GATE_JOB!r} gate's step runs {runs[0]!r}, not {GATE_STEP_RUN!r}. Pinned "
        f"whole rather than as 'something that exits non-zero', because the gate's only "
        f"output is its own failure and there is no reason for that command to be "
        f"anything else. If this is a deliberate rewrite, change it here in the same PR "
        f"— and note that `exit 0` would leave every other check in this section green: "
        f"the condition fires, the step runs, and the required check passes on a red "
        f"dependency."
    )


def check_the_gate_never_continues_on_error(text: str) -> None:
    """Assert nothing in the gate job is allowed to fail quietly.

    Job level and step level both, because both erase the gate: on the step, ``exit 1``
    stops meaning anything; on the job, the gate reports ``success`` no matter what its
    step did. Neither has a legitimate use in a job whose only output is its own failure.
    """
    enabled = [
        f"column {indent}: continue-on-error: {value}"
        for indent, value in continue_on_error(gate_body(text))
        if _is_enabled(value)
    ]
    assert not enabled, (
        f"the {GATE_JOB!r} job sets {enabled}. The gate's only job is to fail — a "
        f"`continue-on-error` anywhere in it turns `exit 1` into a green required check, "
        f"which is red CI merging green."
    )


def check_no_gate_dependency_continues_on_error(text: str) -> None:
    """Assert no job the gate waits on can report a success it did not earn."""
    bodies = job_bodies(text)
    needs = job_needs(bodies.get(GATE_JOB, []))
    assert needs is not None, "unreadable needs line; see test_the_gate_needs_line_is_readable"

    offenders = {
        job: value
        for job in needs
        for indent, value in continue_on_error(bodies.get(job, []))
        if indent == JOB_LEVEL and _is_enabled(value)
    }
    assert not offenders, (
        f"these jobs in the {GATE_JOB!r} gate's `needs` set job-level "
        f"`continue-on-error`: {offenders}. Such a job reports `success` into `needs` "
        f"however it ran, so the gate passes on a job that failed. (A *step's* "
        f"`continue-on-error` is fine and is in use — it is the job-level one that lies.)"
    )


def test_the_fail_open_scanner_read_the_gate():
    """The gate's body was read — otherwise every check below is a vacuous pass."""
    body = gate_body(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert body, (
        f"read no body for the {GATE_JOB!r} job in {CI_WORKFLOW.name} — either the file "
        f"was restructured or job_bodies stopped matching, and the fail-open checks below "
        f"are all asking questions of an empty list"
    )


@pytest.mark.parametrize("workflow", [CI_WORKFLOW, RELEASE_WORKFLOW], ids=lambda w: w.name)
def test_no_continue_on_error_escapes_the_scanner(workflow):
    """No ``continue-on-error`` in either workflow is written in a shape the matcher misses.

    Both files, one body: each has a check that reads only what ``KEY_LINE`` can read, so
    an unreadable spelling has to fail by name here or the switch is simply unchecked.
    """
    unreadable = unreadable_continue_on_error_lines(workflow)
    assert unreadable == [], (
        f"these lines set continue-on-error in a shape KEY_LINE cannot read, so they are "
        f"unchecked — write them as a plain `continue-on-error: <value>` key, or widen "
        f"the matcher: {unreadable}"
    )


def test_the_gate_runs_whatever_its_dependencies_did():
    """``if: always()`` on the gate, the line whose removal makes a failure a skip."""
    check_the_gate_runs_whatever_its_dependencies_did(CI_WORKFLOW.read_text(encoding="utf-8"))


def test_the_gate_step_fails_on_every_bad_result():
    """failure, cancelled and skipped all redden the gate — skipped above all."""
    check_the_gate_step_fails_on_every_bad_result(CI_WORKFLOW.read_text(encoding="utf-8"))


def test_the_gate_never_continues_on_error():
    """Nothing in the gate job may swallow its own `exit 1`."""
    check_the_gate_never_continues_on_error(CI_WORKFLOW.read_text(encoding="utf-8"))


def test_no_gate_dependency_continues_on_error():
    """No required job may report a success it did not earn."""
    check_no_gate_dependency_continues_on_error(CI_WORKFLOW.read_text(encoding="utf-8"))


def test_the_gate_step_actually_fails():
    """`exit 1`, the command the whole gate reduces to."""
    check_the_gate_step_actually_fails(CI_WORKFLOW.read_text(encoding="utf-8"))


def _mutate(text: str, old: str, new: str) -> str:
    """``text`` with one occurrence of ``old`` replaced, asserting the edit applied.

    A mutation test whose mutation silently misses proves nothing worse than nothing: the
    check is handed the untouched file, passes, and ``pytest.raises`` blames the guard for
    what is actually a stale fixture. Fail here instead, naming the string that moved.
    """
    assert text.count(old) == 1, (
        f"expected exactly one {old!r} to mutate, found {text.count(old)}. Either the "
        f"workflow moved, or this string stopped being unique to the job being probed "
        f"(a second job growing an `if: always()` would do it) — re-anchor it on "
        f"surrounding lines. The guard under test is not what failed here."
    )
    return text.replace(old, new, 1)


def _mutate_in_job(text: str, job: str, old: str, new: str) -> str:
    """``text`` with one occurrence of ``old`` replaced **inside one job's block**.

    Anchoring a probe on surrounding lines stops working the moment two jobs are written
    alike, and ``release.yml``'s two publish jobs differ by about three words — a probe
    that silently mutates the wrong one proves nothing about the job it names, and one
    whose anchor matches both trips ``_mutate``'s uniqueness assert and blames the
    workflow for it. Scoping to the block does not care how alike they are.
    """
    lines = text.splitlines(keepends=True)
    # From `jobs:` onward, not the whole file: `on:` and `concurrency:` carry two-space
    # keys too, so a probe named for a job could otherwise land on `  push:`.
    jobs_at = next((index for index, line in enumerate(lines) if line.rstrip() == "jobs:"), None)
    assert jobs_at is not None, "no top-level `jobs:` line — the workflow was restructured"
    header = f"  {job}:\n"
    starts = [index for index, line in enumerate(lines) if line == header and index > jobs_at]
    assert len(starts) == 1, f"expected exactly one {job!r} job, found {len(starts)}"

    start = starts[0] + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if JOB_KEY_SHAPED.match(lines[index]) and not lines[index].lstrip().startswith("#")
        ),
        len(lines),
    )
    # `_mutate` carries the assert-it-applied, so there is one such assertion to improve
    # rather than two that drift — it is the only thing between a stale probe and a
    # `pytest.raises` that reports the guard as broken.
    block = _mutate("".join(lines[start:end]), old, new)
    return "".join(lines[:start]) + block + "".join(lines[end:])


class TestTheFailOpenScannerItself:
    """Where the job-body extraction fires and where it does not, on fabricated files.

    Every case here is a blind spot that would leave the checks above green and empty.
    """

    def test_a_job_body_stops_at_the_next_job(self):
        text = "jobs:\n  lint:\n    runs-on: x\n  ci:\n    if: always()\n"
        assert job_key_value(job_bodies(text)["lint"], "if") is None
        assert job_key_value(job_bodies(text)["ci"], "if") == "always()"

    def test_an_unreadable_job_key_does_not_donate_its_body(self):
        """The neighbour-vouching trap, job edition: a key JOB_KEY cannot read must not
        hand its ``continue-on-error`` to the job written above it."""
        text = 'jobs:\n  lint:\n    runs-on: x\n  "docs":\n    continue-on-error: true\n'
        assert continue_on_error(job_bodies(text)["lint"]) == []

    def test_a_steps_key_is_not_the_jobs_own(self):
        """The column is the whole reading: a step's ``name:``/``if:`` is a different key
        from the job's, and reading one as the other both false-passes and false-fails."""
        text = (
            "jobs:\n"
            "  ci:\n"
            "    name: CI\n"
            "    if: always()\n"
            "    steps:\n"
            "      - name: Fail\n"
            "        if: failure()\n"
        )
        body = job_bodies(text)["ci"]
        assert job_key_value(body, "name") == "CI"
        assert job_key_value(body, "if") == "always()"
        assert step_values(body, "if") == ["failure()"]

    def test_a_job_with_no_steps_has_no_step_values(self):
        assert step_values(job_bodies("jobs:\n  ci:\n    if: always()\n")["ci"], "if") == []

    def test_continue_on_error_is_located_by_column(self):
        """Job level lies to ``needs``; step level is legitimate. Same key, same value."""
        text = (
            "jobs:\n"
            "  ci:\n"
            "    continue-on-error: true\n"
            "    steps:\n"
            "      - run: x\n"
            "        continue-on-error: true\n"
        )
        assert continue_on_error(job_bodies(text)["ci"]) == [(4, "true"), (8, "true")]

    def test_a_dash_led_continue_on_error_is_seen_through(self):
        text = "jobs:\n  ci:\n    steps:\n      - continue-on-error: true\n        run: x\n"
        assert continue_on_error(job_bodies(text)["ci"]) == [(8, "true")]

    def test_an_explicit_false_reads_as_false(self):
        """The one spelling that sets nothing must not be reported as setting something."""
        text = "jobs:\n  ci:\n    continue-on-error: false\n"
        assert continue_on_error(job_bodies(text)["ci"]) == [(4, "false")]

    def test_a_trailing_comment_is_stripped_from_a_value(self):
        text = "jobs:\n  ci:\n    if: always()  # whatever the deps did\n"
        assert job_key_value(job_bodies(text)["ci"], "if") == "always()"

    def test_a_hash_that_follows_no_whitespace_stays_in_the_value(self):
        """YAML's own rule: ``a#b`` is one scalar. Cutting there would shorten real values."""
        assert _strip_comment("issue#243") == "issue#243"

    def test_a_scalar_needs_is_read_as_one_job(self):
        """release.yml's publish jobs are written this way; ci.yml's gate is not."""
        assert job_needs(job_bodies("jobs:\n  a:\n    needs: build\n")["a"]) == ["build"]

    def test_a_flow_mapping_needs_is_reported_not_guessed_at(self):
        assert job_needs(job_bodies("jobs:\n  a:\n    needs: {b: c}\n")["a"]) is None

    def test_reports_a_continue_on_error_it_cannot_read(self, tmp_path):
        """Legal YAML the matcher does not read must be reported, not skipped — that is
        how this check would go green while enforcing nothing."""
        workflow = tmp_path / "fabricated.yml"
        workflow.write_text(
            "jobs:\n  ci:\n    continue-on-error:\n      true\n    steps:\n      - run: x\n"
        )
        assert unreadable_continue_on_error_lines(workflow) == [
            "fabricated.yml:3: continue-on-error:"
        ]

    def test_a_comment_naming_continue_on_error_is_not_reported(self, tmp_path):
        """The over-match limit: prose about the switch is not the switch."""
        workflow = tmp_path / "fabricated.yml"
        workflow.write_text("jobs:\n  ci:\n    # never add continue-on-error here\n")
        assert unreadable_continue_on_error_lines(workflow) == []

    def test_a_run_block_is_not_read_as_structure(self):
        """A heredoc emitting ``key: value`` lines is script, not keys. ``step_values``
        is depth-*bounded*, so without this it reads straight into one."""
        text = (
            "jobs:\n  build:\n    steps:\n      - run: |\n"
            "          cat <<'YML' > out.yml\n"
            "          continue-on-error: true\n"
            "          repository-url: https://evil.invalid/\n"
            "          YML\n"
            "      - uses: actions/checkout@v7\n"
        )
        body = job_bodies(text)["build"]
        assert continue_on_error(body) == []
        assert step_values(body, "repository-url") == []
        assert step_values(body, "uses") == ["actions/checkout@v7"]

    def test_a_dedent_ends_the_block_scalar(self):
        """And not before: a key back at the step's own column is structure again."""
        text = (
            "jobs:\n  build:\n    steps:\n      - run: |\n"
            "          echo hi\n"
            "        continue-on-error: true\n"
        )
        assert continue_on_error(job_bodies(text)["build"]) == [(8, "true")]

    def test_a_blank_line_inside_a_scalar_does_not_end_it(self):
        """Read as a dedent it would put the rest of the script back into the reading.

        Only the blank line, deliberately: a *comment* shallower than the scalar's
        content would end the scalar in YAML itself, which is the same fact
        ``_jobs_block``'s docstring rests its own safety argument on — so a fixture
        asserting otherwise would be enshrining a document that does not parse.
        """
        text = (
            "jobs:\n  build:\n    steps:\n      - run: |\n"
            "          echo one\n"
            "\n"
            "          continue-on-error: true\n"
        )
        assert continue_on_error(job_bodies(text)["build"]) == []

    def test_an_unreadable_continue_on_error_is_left_to_the_blind_spot_guard(self):
        """Reported as an empty value it would read as "not false", and the check would
        accuse a ``false`` of setting the switch it clears. Dropped here, named there."""
        text = "jobs:\n  ci:\n    continue-on-error:\n      false\n"
        assert continue_on_error(job_bodies(text)["ci"]) == []

    @pytest.mark.parametrize("spelling", ["false", "False", "FALSE"])
    def test_every_yaml_false_reads_as_off(self, spelling):
        """All three are the same boolean to YAML 1.2, so none of them is fail-open."""
        assert not _is_enabled(spelling)

    @pytest.mark.parametrize(
        "spelling",
        ["true", "True", "${{ github.event_name }}", '"false"', "'false'", "no", "off"],
        ids=["true", "True", "expression", "quoted", "single-quoted", "no", "off"],
    )
    def test_anything_that_is_not_the_false_boolean_reads_as_on(self, spelling):
        """Quoted, ``false`` is a truthy string to GitHub's evaluator, not the boolean;
        ``no``/``off`` are plain strings under YAML 1.2. And an expression this test
        cannot evaluate is a switch in an unknown position — the direction to fail in."""
        assert _is_enabled(spelling)

    @pytest.mark.parametrize("header", ["|", ">", ">-", "|+", "|2-", ">-2", "|2"])
    def test_every_block_scalar_header_reads_as_unread(self, header):
        """Both orderings of the chomping and indentation indicators: YAML permits
        either, and a reader that knew one would misdiagnose the other as a wrong value.
        """
        assert _is_unread(header)

    def test_a_value_carried_on_the_following_lines_reads_as_unread(self):
        """A plain multi-line scalar leaves nothing on the key's own line. Read as a
        value it is the empty string, which is not "the condition says the wrong thing".
        """
        assert _is_unread("")
        assert not _is_unread("always()")

    @pytest.mark.parametrize("value", ["if:", "if: >-"], ids=["next-line", "block-scalar"])
    def test_an_unread_job_condition_is_reported_as_unread(self, value):
        text = f"jobs:\n  ci:\n    {value}\n      always()\n    steps:\n      - run: exit 1\n"
        with pytest.raises(AssertionError, match="unread, not wrong"):
            check_the_gate_runs_whatever_its_dependencies_did(text)

    @pytest.mark.parametrize("value", ["if:", "if: >-"], ids=["next-line", "block-scalar"])
    def test_an_unread_step_condition_is_reported_as_unread(self, value):
        text = (
            f"jobs:\n  ci:\n    if: always()\n    steps:\n      - {value}\n"
            f"          contains(needs.*.result, 'failure')\n        run: exit 1\n"
        )
        with pytest.raises(AssertionError, match="unread, not wrong"):
            check_the_gate_step_fails_on_every_bad_result(text)

    def test_a_block_scalar_needs_is_not_a_job_named_dash(self):
        """Handed back as a value it is a dependency on a job literally named ``>-``,
        which defeats the "unreadable needs is its own failure" guard entirely."""
        assert job_needs(job_bodies("jobs:\n  ci:\n    needs: >-\n      lint\n")["ci"]) is None

    @pytest.mark.parametrize(
        "condition",
        ["always()", "${{ always() }}", '"always()"', "${{  always()  }}"],
        ids=["bare", "wrapped", "quoted", "loosely-spaced"],
    )
    def test_the_spellings_of_one_condition_read_alike(self, condition):
        """GitHub reads these identically; a reader that knew one would call the others a
        disabled gate, which is a red CI over an edit that changed nothing."""
        assert _unwrap_expression(condition) == "always()"

    def test_a_hash_inside_a_quoted_scalar_is_not_a_comment(self):
        """YAML's other rule about ``#``. Cutting here would read a value Actions never
        sees — a quoted job name, environment name or condition, silently shortened."""
        assert _strip_comment('"CI # 1"') == '"CI # 1"'

    def test_a_repeated_job_key_does_not_splice_two_blocks(self):
        """Invalid YAML that actionlint rejects first — but splicing the two bodies would
        let either donate keys to the other, which is the merge this refuses by design."""
        text = (
            "jobs:\n  a:\n    continue-on-error: true\n  b:\n    runs-on: x\n  a:\n    runs-on: y\n"
        )
        assert job_bodies(text)["a"] == ["    runs-on: y"]

    def test_reports_a_continue_on_error_whose_value_is_only_a_comment(self, tmp_path):
        """The trap that defeated both halves at once: readable to a raw-value test,
        empty to the reader, and the switch is set on the line beneath."""
        workflow = tmp_path / "fabricated.yml"
        workflow.write_text("jobs:\n  ci:\n    continue-on-error:  # while we debug\n      true\n")
        assert unreadable_continue_on_error_lines(workflow) == [
            "fabricated.yml:3: continue-on-error:  # while we debug"
        ]

    def test_a_key_whose_only_value_is_a_comment_has_no_value(self):
        """KEY_LINE eats the whitespace ahead of the value, so the ``#`` arrives first on
        the line. Read as a value it would be a phantom job in the gate's ``needs``."""
        text = "jobs:\n  ci:\n    needs:  # the block list below\n      - lint\n"
        assert job_needs(job_bodies(text)["ci"]) is None


class TestEachFailOpenEditIsCaught:
    """Every known one-line edit that fails the gate open, applied to the real ``ci.yml``
    and each rejected by its own check — the three #243 enumerated plus the ``exit 0``
    #250 added, and the false-alarm directions beside them.

    Fabricated fragments prove where the scanner looks; these prove it is looking at
    *this* workflow, so a rename or a reflow cannot quietly aim the checks at nothing.
    Deliberately not a numbered count: the last two times this section claimed one, the
    number was what went stale.
    """

    @pytest.fixture(scope="module")
    @classmethod
    def ci(cls):
        return CI_WORKFLOW.read_text(encoding="utf-8")

    def test_dropping_the_gates_always_is_caught(self, ci):
        # Anchored on the line below it, so the probe stays the gate's even if another
        # job grows an `if: always()` of its own.
        broken = _mutate(ci, "    if: always()\n    steps:\n", "    steps:\n")
        with pytest.raises(AssertionError, match="skips the gate"):
            check_the_gate_runs_whatever_its_dependencies_did(broken)

    def test_narrowing_the_gates_step_condition_to_failure_is_caught(self, ci):
        broken = _mutate(ci, GATE_STEP_IF, f"contains(needs.*.result, '{GATE_FAILING_RESULTS[0]}')")
        with pytest.raises(AssertionError, match=r"does not name \['cancelled', 'skipped'\]"):
            check_the_gate_step_fails_on_every_bad_result(broken)

    def test_a_continue_on_error_on_the_gates_step_is_caught(self, ci):
        broken = _mutate(
            ci, "        run: exit 1", "        continue-on-error: true\n        run: exit 1"
        )
        with pytest.raises(AssertionError, match="column 8"):
            check_the_gate_never_continues_on_error(broken)

    def test_a_continue_on_error_on_the_gate_job_is_caught(self, ci):
        broken = _mutate(
            ci,
            "    if: always()\n    steps:\n",
            "    if: always()\n    continue-on-error: true\n    steps:\n",
        )
        with pytest.raises(AssertionError, match="column 4"):
            check_the_gate_never_continues_on_error(broken)

    def test_a_dependency_that_continues_on_error_is_caught(self, ci):
        broken = _mutate(
            ci,
            "  lint:\n    name: lint\n",
            "  lint:\n    name: lint\n    continue-on-error: true\n",
        )
        with pytest.raises(AssertionError, match="'lint': 'true'"):
            check_no_gate_dependency_continues_on_error(broken)

    def test_a_dependency_really_does_carry_a_step_level_continue_on_error(self, ci):
        """The false-alarm direction is live, not hypothetical: one of the gate's
        dependencies really does carry a step-level ``continue-on-error`` (the advisory
        pass over the capital's stubs), so the column distinction is exercised against
        this workflow and not only against fabricated fragments.

        Read from whichever job has one rather than by name, so renaming that job is not
        a failure of the gate's test. That the check stays green with it there is
        ``test_no_gate_dependency_continues_on_error``'s assertion, not this one's —
        making it here as well would report one regression as two failures.

        If this ever fails because the last step-level ``continue-on-error`` in
        ``ci.yml`` was removed, retire this test with it, the way
        ``test_the_scanner_finds_the_upload_steps`` says to.
        """
        bodies = job_bodies(ci)
        needs = job_needs(gate_body(ci))
        assert needs is not None, "unreadable needs line; see test_the_gate_needs_line_is_readable"
        step_level = [
            (job, value)
            for job in needs
            for indent, value in continue_on_error(bodies.get(job, []))
            if indent > JOB_LEVEL
        ]
        assert step_level, (
            "no dependency of the gate carries a step-level `continue-on-error` any "
            "more, so nothing proves this check tolerates one against the real ci.yml"
        )

    def test_renaming_the_gates_reported_check_is_still_caught(self, ci):
        """#238's guard, re-proved: this change rewired ``gate_check_name`` off its own
        regex onto ``KEY_LINE``, and a reader that stopped finding the gate's ``name:``
        would leave ``test_the_gate_reports_under_the_name_branch_protection_requires``
        green while the required check never arrived.
        """
        assert gate_check_name(ci) == GATE_CHECK_NAME
        broken = _mutate(ci, "  ci:\n    name: CI\n", "  ci:\n    name: CI (required)\n")
        assert gate_check_name(broken) == "CI (required)"

    def test_swapping_the_conditions_or_for_and_is_caught(self, ci):
        """One character, all three results still named, and the step now fires only when
        a failure, a cancellation and a skip all happen at once."""
        broken = _mutate(ci, GATE_STEP_IF, GATE_STEP_IF.replace(" || ", " && "))
        with pytest.raises(AssertionError, match="operators are what changed"):
            check_the_gate_step_fails_on_every_bad_result(broken)

    def test_inverting_the_gates_step_condition_is_caught(self, ci):
        broken = _mutate(ci, GATE_STEP_IF, f"!({GATE_STEP_IF})")
        with pytest.raises(AssertionError, match="operators are what changed"):
            check_the_gate_step_fails_on_every_bad_result(broken)

    def test_neutering_the_gates_command_is_caught(self, ci):
        """The edit #243's enumeration missed: the condition still fires, the step still
        runs, and the gate reports success on every red dependency."""
        broken = _mutate(ci, "        run: exit 1", "        run: exit 0")
        with pytest.raises(AssertionError, match="runs 'exit 0'"):
            check_the_gate_step_actually_fails(broken)

        # All four of #243's gate checks pass on it, pinned so the enumeration in this
        # module's docstring stays true rather than being asserted three-quarters of the
        # way and read as complete.
        check_the_gate_runs_whatever_its_dependencies_did(broken)
        check_the_gate_step_fails_on_every_bad_result(broken)
        check_the_gate_never_continues_on_error(broken)
        check_no_gate_dependency_continues_on_error(broken)

    def test_the_wrapped_spelling_of_always_is_accepted(self, ci):
        """The false-alarm direction for the gate's own condition: ``${{ always() }}`` is
        the same gate, and the form GitHub's own documentation writes."""
        rewritten = _mutate(
            ci, "    if: always()\n    steps:\n", "    if: ${{ always() }}\n    steps:\n"
        )
        check_the_gate_runs_whatever_its_dependencies_did(rewritten)


# ---------------------------------------------------------------------------------------
# #243: release.yml's contractual facts
# ---------------------------------------------------------------------------------------
#
# PyPI's Trusted Publisher is not a credential this repo holds — it is a registration on
# PyPI naming an owner, a repository, a **workflow filename** and an **environment name**
# (CLAUDE.md -> Releasing, "Contractual names"). Nothing in the repository enforced that
# half of the contract, and the cost of breaking it is asymmetric: the mismatch surfaces
# as a 403 at the publish step, on a tag that is already pushed, for a version number
# PyPI will never release back. These pins are the registration, written down where a PR
# reads it. (``RELEASE_WORKFLOW`` itself is declared beside ``CI_WORKFLOW`` at the top.)

#: The TestPyPI rehearsal and the real publish. The rehearsal must finish before the
#: real publish starts, or it is not a rehearsal.
REHEARSAL_JOB = "publish-testpypi"
PUBLISH_JOB = "publish-pypi"

#: One list of the jobs that publish. Every check below iterates this rather than the
#: environment map: a job that published under no environment would otherwise drop out
#: of the OIDC and index checks with nothing failing.
PUBLISH_JOBS = (REHEARSAL_JOB, PUBLISH_JOB)

#: Trusted Publishing authenticates with a short-lived OIDC token, and Actions mints one
#: only for a job that asks. Without this the publish 403s for want of a token at all —
#: the same unrecoverable moment as a renamed environment, reached by deleting two lines
#: that look like boilerplate (#250).
OIDC_PERMISSION_KEY, OIDC_PERMISSION_VALUE = "id-token", "write"

#: The index the rehearsal publishes to. Its **absence** on the real publish is what makes
#: that one the real index, so both halves are pinned: delete this input and the rehearsal
#: uploads to pypi.org under the `testpypi` environment, which carries no protection
#: rules — the release ships before the capital ever actuates the `pypi` gate (#250).
TESTPYPI_REPOSITORY_URL = "https://test.pypi.org/legacy/"

#: Both spellings of that input. ``pypa/gh-action-pypi-publish`` still honours the
#: underscored alias, so reading only the hyphen would let a ``repository_url:`` aim the
#: real publish anywhere at all while the check that it names no index stayed green.
REPOSITORY_URL_KEYS = ("repository-url", "repository_url")

#: The environment each publish job is registered under. Per job, not as a set: swapping
#: the two names would send the real release to the rehearsal index and rehearse against
#: PyPI, which is the worse direction of the same edit.
RELEASE_ENVIRONMENTS = dict(zip(PUBLISH_JOBS, ("testpypi", "pypi")))


def level_mapping(lines: list[str], key: str, level: int) -> dict[str, str] | None:
    """A job-level key's nested one-level mapping, or ``None`` if absent or unreadable.

    ``environment:`` and ``permissions:`` are both written this way, so one reader serves
    both rather than two walks of the same shape drifting apart.

    Two boundaries carry the correctness. The mapping ends where the lines dedent back to
    the job's own keys. And its entries are taken at the **child column of its first
    child**, read rather than assumed: bounded only by ``JOB_LEVEL``, a key nested under
    one of the mapping's own *values* would be returned as if the mapping had declared
    it, passing a check on a job that declared nothing.

    A sequence under the key reads as ``None``: Actions takes a string or a mapping in
    both of these places and rejects a list, so reading entries out of one would vouch
    for a workflow that does not load at all. So does a scalar — that is the shorthand
    spelling, and it is the caller's to interpret, not a mapping with no entries.
    """
    entries: dict[str, str] = {}
    child_column: int | None = None
    inside = False

    for line in lines:
        match = KEY_LINE.match(line)
        if not inside:
            if match and match.group("key") == key and _key_indent(line) == level:
                if _strip_comment(match.group("value") or ""):
                    return None
                inside = True
            continue

        if not _is_structural(line):
            continue
        if _key_indent(line) <= level:
            break  # dedented back out to the owning level's keys
        if _dash_column(line) is not None:
            return None  # a sequence where Actions takes a mapping
        if child_column is None:
            child_column = _key_indent(line)
        if _key_indent(line) != child_column:
            continue  # deeper: inside one of the mapping's own values
        if match:
            entries[match.group("key")] = _strip_comment(match.group("value") or "").strip("\"'")

    return entries if inside else None


def job_mapping(body: list[str], key: str) -> dict[str, str] | None:
    """A job-level key's nested mapping — the job-level reading of ``level_mapping``."""
    return level_mapping(body, key, JOB_LEVEL)


def job_environment(body: list[str]) -> str | None:
    """A job's deployment environment, or ``None`` if it declares none this reader knows.

    Both legal spellings, because either could be written and neither is wrong: the
    ``environment: pypi`` shorthand, and the mapping form with a nested ``name:`` that
    ``release.yml`` uses because it also carries a ``url:``.
    """
    shorthand = job_key_value(body, "environment")
    if shorthand:
        # Flow style (`environment: {name: pypi}`) is legal YAML this reader does not
        # parse, and handing the braces back as a name would accuse a correct release.yml
        # of naming an environment PyPI never registered. Refused, the way
        # _parse_needs_value refuses the same punctuation.
        if _is_unread(shorthand) or any(character in shorthand for character in "[]{},"):
            return None
        return shorthand.strip("\"'")

    mapping = job_mapping(body, "environment")
    return None if mapping is None else mapping.get("name")


def check_the_publish_jobs_declare_the_registered_environments(text: str) -> None:
    """Assert each publish job still names the environment PyPI trusts it under."""
    bodies = job_bodies(text)
    for job, environment in RELEASE_ENVIRONMENTS.items():
        assert job in bodies, (
            f"no {job!r} job in {RELEASE_WORKFLOW.name}. Its environment is half of a "
            f"registration on PyPI that nobody in this repo can edit — renaming or "
            f"removing the job means updating the trusted publisher first."
        )
        declared = job_environment(bodies[job])
        assert declared == environment, (
            f"{job!r} declares environment {declared!r}, but PyPI's trusted publisher is "
            f"registered against {environment!r}. A mismatch is a 403 at the publish "
            f"step, on a tag already pushed and a version number PyPI never releases "
            f"back (CLAUDE.md -> Releasing). (None means this reader found no environment "
            f"it could read — if one is declared in a shape it does not know, teach it "
            f"that shape rather than leaving the contract unchecked.)"
        )


def check_the_real_publish_waits_on_the_rehearsal(text: str) -> None:
    """Assert PyPI is only ever reached through a green TestPyPI rehearsal."""
    needs = job_needs(job_bodies(text).get(PUBLISH_JOB, []))
    assert needs == [REHEARSAL_JOB], (
        f"{PUBLISH_JOB!r} needs {needs}, not [{REHEARSAL_JOB!r}]. Without that edge the "
        f"two publishes race: the rehearsal stops being a rehearsal, and a package that "
        f"fails on TestPyPI is already on PyPI under a filename that cannot be replaced."
    )


#: A ``uses:`` pulling in the PyPI publish action, however it is referenced — the same
#: loose matching ``UPLOAD_ACTION`` uses, for the same reason: a SHA pin or a fork is
#: still the step that uploads.
PUBLISH_ACTION = re.compile(
    r"^\s*(?:-\s+)?uses:\s*[\"']?(?P<action>\S*gh-action-pypi-publish[^\s\"']*)"
)


def publish_steps(body: list[str]) -> list[list[str]]:
    """Every ``gh-action-pypi-publish`` step in a job body, each as its own lines.

    Attributing ``repository-url`` to the **step** rather than to the job is the whole
    point, and it is the same trap ``_step_containing`` was written for on the retention
    half: read job-wide, the input can sit on a neighbouring step — the artifact download,
    say — and the check still passes while the publish step itself has none and uploads
    to pypi.org.
    """
    # Script lines skipped, like every other reader here: a `run: |` body that greps
    # for the action, or writes it into a file, is shell — counting it as a second
    # publish step would redden CI over an echo.
    inside_a_script = block_scalar_line_numbers(body)
    return [
        _step_containing(body, index)
        for index, line in enumerate(body)
        if index not in inside_a_script and PUBLISH_ACTION.match(line)
    ]


def publish_repository_url(steps: list[list[str]]) -> list[str]:
    """The index inputs declared on these publish steps, either spelling, quotes stripped.

    Takes the steps rather than the job, so the caller's "exactly one publish step" guard
    and this reading are demonstrably about the same step rather than two independent
    re-derivations of it.
    """
    return [
        value.strip("\"'")
        for step in steps
        for key in REPOSITORY_URL_KEYS
        for value in step_values(step, key)
    ]


def unreadable_repository_url_lines(workflow: Path) -> list[str]:
    """Every naming of an index input, outside a comment, the matcher cannot read.

    The blind-spot guard for the one *fail-open* direction in this section: the real
    publish must name **no** index, and an empty read is indistinguishable from one
    written in a shape ``KEY_LINE`` misses. Without this, flow style or a value on the
    next line aims the real publish anywhere at all, green.
    """
    return unreadable_key_lines(workflow, *REPOSITORY_URL_KEYS)


#: Actions' two ``permissions:`` shorthands. ``write-all`` grants everything, ``id-token``
#: included, so a job written that way does mint a token — calling it a failure would be
#: a red CI over a workflow that publishes perfectly well.
PERMISSIONS_GRANTING_EVERYTHING = "write-all"
PERMISSIONS_GRANTING_NOTHING = "read-all"


def oidc_grant(lines: list[str], level: int) -> tuple[bool | None, str] | None:
    """Whether the ``permissions:`` declared at ``level`` mints an OIDC token.

    Three outcomes, kept apart because they are three different edits. The **outer**
    ``None`` means nothing is declared at this level at all — the caller decides what
    Actions would fall back to. Inside the tuple, a ``None`` means declared in a shape
    this reader does not resolve, which is not the same as denied, and must never be
    reported as one.
    """
    declared = level_key_value(lines, "permissions", level)
    if declared is None:
        return None

    if declared:
        value = declared.strip("\"'")
        if value == PERMISSIONS_GRANTING_EVERYTHING:
            return True, f"`permissions: {value}`, which grants everything"
        if value == PERMISSIONS_GRANTING_NOTHING:
            return False, f"`permissions: {value}`, which grants nothing writable"
        return None, f"`permissions: {value}`, a shorthand this reader does not resolve"

    mapping = level_mapping(lines, "permissions", level)
    if mapping is None:
        return None, "a `permissions:` written in a shape this reader does not resolve"

    granted = mapping.get(OIDC_PERMISSION_KEY)
    described = f"a `permissions:` mapping setting `{OIDC_PERMISSION_KEY}: {granted}`"
    if granted is None:
        return False, f"a `permissions:` mapping granting only {sorted(mapping)}"
    if granted in (OIDC_PERMISSION_VALUE, "read", "none"):
        return granted == OIDC_PERMISSION_VALUE, described
    # An expression, or a value Actions does not define: unresolved, not denied -- the
    # same reading the shorthand branch above takes of the same class of input.
    return None, f"{described}, which this reader does not resolve"


def check_the_publish_jobs_can_mint_an_oidc_token(text: str) -> None:
    """Assert each publish job can still get the OIDC token Trusted Publishing needs.

    Job level first, then the workflow's, because that is Actions' own rule: a job
    declaring any ``permissions:`` of its own replaces the workflow's outright, and only
    a job declaring none inherits. Reading the job alone would call a correct workflow
    broken the day someone hoists the grant to the top — and "the publish fails for want
    of a token" is not a sentence to be wrong about.
    """
    bodies = job_bodies(text)
    # Read once: the workflow's grant is a single fact, and two reads of one fact are
    # two things a later edit can make disagree.
    inherited = oidc_grant(text.splitlines(), 0)

    for job in PUBLISH_JOBS:
        # An absent job and a job with no `permissions:` both read as None below, and
        # only one of them is a workflow that publishes. Without this the check reports
        # a renamed job as inheriting a grant it is not there to inherit.
        assert job in bodies, (
            f"no {job!r} job in the release workflow, so there is nothing here to ask "
            f"about its OIDC token. If the job was renamed, PyPI's trusted publisher "
            f"has to be updated with it before this check is."
        )

        grant = oidc_grant(bodies[job], JOB_LEVEL)
        where = "the job's"
        if grant is None:
            grant = inherited
            where = "the workflow's (the job declares none, so Actions falls back to it)"
        if grant is None:
            grant = (False, "no `permissions:` at all")
            where = "neither the job nor the workflow declares"

        granted, described = grant
        assert granted is not None, (
            f"cannot tell whether {job!r} can mint an OIDC token: {where} permissions are "
            f"{described}. Unresolved is not the same as denied — teach this reader that "
            f"shape rather than leaving the contract unchecked, or write the mapping."
        )
        assert granted, (
            f"{job!r} cannot mint an OIDC token: {where} permissions are {described}, and "
            f"Trusted Publishing has no other credential. Actions mints the token only "
            f"for a job that asks, so the publish 403s on a tag already pushed, for a "
            f"version PyPI never releases back."
        )


def check_the_rehearsal_publishes_to_the_rehearsal_index(text: str) -> None:
    """Assert the TestPyPI rehearsal uploads to TestPyPI, and the real publish does not.

    Both halves, because the second is what makes the first meaningful: the real publish
    names no ``repository-url`` at all — its *absence* is what selects pypi.org — so a
    rehearsal that also names none is not a rehearsal, it is a second real publish, run
    under an environment with no protection rules and before the ``pypi`` gate.
    """
    bodies = job_bodies(text)
    steps = {}
    for job in PUBLISH_JOBS:
        steps[job] = publish_steps(bodies.get(job, []))
        assert len(steps[job]) == 1, (
            f"expected exactly one gh-action-pypi-publish step in {job!r}, found "
            f"{len(steps[job])}. The readings below are about that step; with none, or "
            f"more than one, there is no single step for them to be about."
        )

    rehearsal = publish_repository_url(steps[REHEARSAL_JOB])
    assert rehearsal == [TESTPYPI_REPOSITORY_URL], (
        f"{REHEARSAL_JOB!r} names repository-url {rehearsal}, not "
        f"[{TESTPYPI_REPOSITORY_URL!r}]. Unset, `pypa/gh-action-pypi-publish` uploads to "
        f"**pypi.org** — so the rehearsal publishes the real release, under the "
        f"{RELEASE_ENVIRONMENTS[REHEARSAL_JOB]!r} environment, which carries no "
        f"protection rules, before the `pypi` gate is ever actuated."
    )

    real = publish_repository_url(steps[PUBLISH_JOB])
    assert real == [], (
        f"{PUBLISH_JOB!r} names repository-url {real}. The real publish names none — its "
        f"absence is what selects pypi.org — so anything here is either a redundant "
        f"restatement or a publish pointed somewhere nobody expects."
    )


def check_no_release_job_continues_on_error(text: str) -> None:
    """Assert no job in the release pipeline can report a success it did not earn.

    Every job here, not only the publishes: each ``needs`` edge in this workflow is load
    bearing, and a job that continues on error reports ``success`` into the one naming
    it. On ``publish-testpypi`` that turns the rehearsal edge into decoration — a package
    TestPyPI rejected goes to PyPI anyway, which is the exact outcome the edge's own
    check promises to prevent.

    At **any** column, unlike the ci.yml check, and the difference is not an oversight:
    there, ``actionlint``'s advisory pass over the capital's stubs is a legitimate
    step-level use, so that check has to read the column. Here there is none — every step
    in this pipeline either builds the artifact, guards the tag, or publishes, and a step
    allowed to fail quietly neuters whichever of those it is. Same reasoning as
    ``check_the_gate_never_continues_on_error``.
    """
    # A list, not a mapping: two offenders in one job at one column are two edits to
    # make, and a dict keyed by job-and-column would report the second as the first.
    bodies = job_bodies(text)
    # "found no offenders" and "read no jobs" are the same empty answer, and only one of
    # them is a clean workflow -- the positive-read guard every other scanner here has.
    assert bodies, (
        "read no jobs in the release workflow, so this check is asking nothing of it — "
        "either the file was restructured or JOB_KEY stopped matching"
    )

    offenders = [
        f"{job} (column {indent}): continue-on-error: {value}"
        for job, body in bodies.items()
        for indent, value in continue_on_error(body)
        if _is_enabled(value)
    ]
    assert not offenders, (
        f"these set `continue-on-error` in the release workflow: {offenders}. At "
        f"column {JOB_LEVEL} the job reports `success` into every `needs` naming it, "
        f"however it ran; deeper, the step it guards stops being able to stop the "
        f"pipeline. Either way a failed build or a rejected rehearsal still reaches "
        f"PyPI, under a filename that can never be replaced."
    )


def test_the_release_workflow_is_where_pypi_expects_it():
    """The filename is contractual — PyPI's trusted publisher is registered against it.

    Renaming the file is not a refactor. The publish would 403 with a trust error at the
    one moment nothing can be undone: the tag is pushed, the version is spent, and fixing
    it needs a change on PyPI that no actor in this repo can make.
    """
    assert RELEASE_WORKFLOW.is_file(), (
        f"{RELEASE_WORKFLOW.name} is missing from {WORKFLOWS}. PyPI's trusted publisher "
        f"names this filename (CLAUDE.md -> Releasing, 'Contractual names'); if it truly "
        f"moved, the pending publisher on PyPI and TestPyPI has to move with it."
    )


def test_the_release_scanner_read_the_workflow():
    """The extraction found the publish jobs — otherwise the checks below are vacuous."""
    jobs = job_bodies(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    missing = [job for job in RELEASE_ENVIRONMENTS if job not in jobs]
    assert not missing, (
        f"read {sorted(jobs)} as the jobs in {RELEASE_WORKFLOW.name} and did not find "
        f"{missing} — either the file was restructured or job_bodies stopped matching, "
        f"and the contractual checks below are asking about jobs that were never read"
    )


def test_the_publish_jobs_declare_the_registered_environments():
    """``testpypi`` and ``pypi``, on the jobs PyPI registered them against."""
    check_the_publish_jobs_declare_the_registered_environments(
        RELEASE_WORKFLOW.read_text(encoding="utf-8")
    )


def test_the_real_publish_waits_on_the_rehearsal():
    """PyPI is reachable only through a green TestPyPI rehearsal."""
    check_the_real_publish_waits_on_the_rehearsal(RELEASE_WORKFLOW.read_text(encoding="utf-8"))


def test_the_publish_jobs_can_mint_an_oidc_token():
    """`id-token: write` — the whole of Trusted Publishing's authentication."""
    check_the_publish_jobs_can_mint_an_oidc_token(RELEASE_WORKFLOW.read_text(encoding="utf-8"))


def test_the_rehearsal_publishes_to_the_rehearsal_index():
    """The rehearsal goes to TestPyPI, and the real publish names no index at all."""
    check_the_rehearsal_publishes_to_the_rehearsal_index(
        RELEASE_WORKFLOW.read_text(encoding="utf-8")
    )


def test_no_release_job_continues_on_error():
    """No `needs` edge in the publish pipeline may be satisfied by a job that failed."""
    check_no_release_job_continues_on_error(RELEASE_WORKFLOW.read_text(encoding="utf-8"))


def test_no_repository_url_escapes_the_scanner():
    """The fail-open direction needs its own guard: the real publish must name **no**
    index, and "none" is what an unreadable spelling also looks like."""
    unreadable = unreadable_repository_url_lines(RELEASE_WORKFLOW)
    assert unreadable == [], (
        f"these lines name repository-url in a shape KEY_LINE cannot read, so the check "
        f"that the real publish declares none cannot tell them from absence — write them "
        f"as a plain `repository-url: <value>` key, or widen the matcher: {unreadable}"
    )


class TestTheReleaseScannerItself:
    """Where ``job_mapping``, ``job_environment`` and the publish-step reading fire, and
    where they do not. ``environment:`` and ``permissions:`` are the same nested shape and
    share one reader, so they share one class."""

    def test_reads_the_mapping_form_release_yml_uses(self):
        text = "jobs:\n  publish:\n    environment:\n      name: pypi\n      url: https://x\n"
        assert job_environment(job_bodies(text)["publish"]) == "pypi"

    def test_reads_the_shorthand_form(self):
        text = "jobs:\n  publish:\n    environment: pypi\n"
        assert job_environment(job_bodies(text)["publish"]) == "pypi"

    def test_a_url_before_the_name_is_stepped_over(self):
        text = "jobs:\n  publish:\n    environment:\n      url: https://x\n      name: pypi\n"
        assert job_environment(job_bodies(text)["publish"]) == "pypi"

    def test_the_environment_is_not_taken_from_the_next_job(self):
        """The block-boundary trap: a mapping with no ``name:`` must read as unreadable,
        not reach forward into whatever the following job declares."""
        text = (
            "jobs:\n"
            "  publish:\n    environment:\n      url: https://x\n"
            "  other:\n    environment:\n      name: pypi\n"
        )
        assert job_environment(job_bodies(text)["publish"]) is None

    def test_a_job_with_no_environment_reads_as_none(self):
        assert job_environment(job_bodies("jobs:\n  build:\n    runs-on: x\n")["build"]) is None

    def test_a_steps_environment_key_is_not_the_jobs(self):
        text = "jobs:\n  publish:\n    steps:\n      - environment: pypi\n"
        assert job_environment(job_bodies(text)["publish"]) is None

    @pytest.mark.parametrize("value", ["|", ">-"], ids=["literal", "folded"])
    def test_a_block_scalar_environment_is_refused_not_read_as_a_name(self, value):
        text = f"jobs:\n  publish:\n    environment: {value}\n      pypi\n"
        assert job_environment(job_bodies(text)["publish"]) is None

    def test_a_sequence_under_environment_is_refused(self):
        """Actions takes a string or a mapping here and rejects a list, so reading a
        ``name:`` out of one would vouch for a release.yml that does not load at all."""
        text = "jobs:\n  publish:\n    environment:\n      - name: pypi\n"
        assert job_environment(job_bodies(text)["publish"]) is None

    def test_reads_a_permissions_mapping(self):
        text = "jobs:\n  publish:\n    permissions:\n      id-token: write\n      contents: read\n"
        assert job_mapping(job_bodies(text)["publish"], "permissions") == {
            "id-token": "write",
            "contents": "read",
        }

    def test_a_key_under_one_of_the_mappings_values_is_not_the_mappings_own(self):
        """Bounded by the mapping's child column, not the job's: otherwise this job —
        which grants nothing at its own level — would read as granting `id-token`."""
        text = "jobs:\n  publish:\n    permissions:\n      contents:\n        id-token: write\n"
        assert job_mapping(job_bodies(text)["publish"], "permissions") == {"contents": ""}

    def test_a_mapping_stops_at_the_next_job_level_key(self):
        text = "jobs:\n  publish:\n    permissions:\n      id-token: write\n    steps:\n"
        assert job_mapping(job_bodies(text)["publish"], "permissions") == {"id-token": "write"}

    def test_an_absent_mapping_reads_as_none_not_empty(self):
        """ "grants nothing" and "grants an empty mapping" are different claims, and only
        one of them is a job that never asked."""
        assert job_mapping(job_bodies("jobs:\n  p:\n    steps:\n")["p"], "permissions") is None

    def test_a_scalar_where_a_mapping_was_expected_reads_as_none(self):
        """`permissions: read-all` is legal and grants no id-token; handing back an empty
        mapping would say the same thing by accident rather than on purpose."""
        text = "jobs:\n  p:\n    permissions: read-all\n"
        assert job_mapping(job_bodies(text)["p"], "permissions") is None

    def test_a_sequence_where_a_mapping_was_expected_reads_as_none(self):
        text = "jobs:\n  p:\n    permissions:\n      - id-token: write\n"
        assert job_mapping(job_bodies(text)["p"], "permissions") is None

    def test_step_values_reaches_a_with_input(self):
        """``repository-url`` is a ``with:`` input, a level deeper than a step's own keys
        — one reader has to see both, or the release pins need a second one."""
        text = (
            "jobs:\n  publish:\n    steps:\n      - uses: pypa/gh-action-pypi-publish@v1\n"
            "        with:\n          repository-url: https://test.pypi.org/legacy/\n"
        )
        body = job_bodies(text)["publish"]
        assert step_values(body, "repository-url") == ["https://test.pypi.org/legacy/"]
        assert step_values(body, "uses") == ["pypa/gh-action-pypi-publish@v1"]

    def test_an_input_on_a_neighbouring_step_is_not_the_publish_steps(self):
        """The attribution trap, and the worst false pass available here: read job-wide,
        this reads as a rehearsal pointed at TestPyPI while the step that actually
        publishes names no index and uploads to pypi.org."""
        text = (
            "jobs:\n  publish:\n    steps:\n      - uses: actions/download-artifact@v8\n"
            "        with:\n          repository-url: https://test.pypi.org/legacy/\n"
            "      - uses: pypa/gh-action-pypi-publish@release/v1\n"
        )
        body = job_bodies(text)["publish"]
        assert step_values(body, "repository-url") == ["https://test.pypi.org/legacy/"]
        assert publish_repository_url(publish_steps(body)) == []

    @pytest.mark.parametrize(
        "reference",
        [
            "pypa/gh-action-pypi-publish@release/v1",
            '"pypa/gh-action-pypi-publish@release/v1"',
            "pypa/gh-action-pypi-publish@d4f7e1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8",
            "someorg/gh-action-pypi-publish-fork@v1",
        ],
        ids=["tagged", "quoted", "sha-pinned", "third-party-fork"],
    )
    def test_the_publish_step_is_found_however_the_action_is_referenced(self, reference):
        """Loose on the reference, like ``UPLOAD_ACTION``: a fork or a SHA pin is still
        the step that uploads."""
        text = f"jobs:\n  publish:\n    steps:\n      - uses: {reference}\n"
        assert len(publish_steps(job_bodies(text)["publish"])) == 1

    def test_a_quoted_repository_url_is_the_same_index(self):
        """Quotes are legal YAML around a scalar and say nothing about the value —
        reddening CI over them would be an error message untrue of the edit."""
        text = (
            "jobs:\n  publish:\n    steps:\n      - uses: pypa/gh-action-pypi-publish@v1\n"
            '        with:\n          repository-url: "https://test.pypi.org/legacy/"\n'
        )
        body = job_bodies(text)["publish"]
        assert publish_repository_url(publish_steps(body)) == ["https://test.pypi.org/legacy/"]

    @pytest.mark.parametrize(
        "line",
        ["        with: {repository-url: https://x/}", "          repository-url:"],
        ids=["flow-style", "value-on-the-next-line"],
    )
    def test_reports_a_repository_url_it_cannot_read(self, tmp_path, line):
        workflow = tmp_path / "fabricated.yml"
        workflow.write_text(f"jobs:\n  publish:\n    steps:\n{line}\n")
        assert len(unreadable_repository_url_lines(workflow)) == 1

    def test_a_comment_naming_repository_url_is_not_reported(self, tmp_path):
        workflow = tmp_path / "fabricated.yml"
        workflow.write_text("jobs:\n  publish:\n    # never set repository-url here\n")
        assert unreadable_repository_url_lines(workflow) == []

    def test_a_flow_style_environment_is_refused_not_read_as_a_name(self):
        """Legal YAML this reader does not parse. Handing back the braces would accuse a
        correct release.yml of naming an environment PyPI never registered."""
        text = "jobs:\n  publish:\n    environment: {name: pypi}\n"
        assert job_environment(job_bodies(text)["publish"]) is None

    def test_a_name_under_one_of_the_mappings_values_is_not_the_environment(self):
        """Bounded by the mapping's own child column, not by the job's: otherwise this
        job — which declares no environment name at all — would read as ``pypi`` and the
        contract check would go green on a workflow that 403s."""
        text = (
            "jobs:\n  publish:\n    environment:\n      url: https://x\n"
            "      extra:\n        name: pypi\n"
        )
        assert job_environment(job_bodies(text)["publish"]) is None

    def test_a_commented_environment_key_still_descends_to_the_nested_name(self):
        """The comment is not the environment name. Reading it as one would redden CI
        claiming PyPI's trusted publisher is misregistered, on a release.yml that is
        exactly right."""
        text = "jobs:\n  publish:\n    environment:  # actuated by the capital\n      name: pypi\n"
        assert job_environment(job_bodies(text)["publish"]) == "pypi"


def _publish_step_line(release: str) -> str:
    """The real publish job's ``uses:`` line, read off the file, newline included.

    Read rather than written out, because the line carries a SHA pin that Dependabot
    rewrites (#339): spelled here, every bump would redden these tests and never merge.
    """
    steps = publish_steps(job_bodies(release)[PUBLISH_JOB])
    assert len(steps) == 1, f"{PUBLISH_JOB!r} has {len(steps)} publish steps, not one"
    (line,) = (line for line in steps[0] if PUBLISH_ACTION.match(line))
    return line + "\n"


class TestEachReleaseEditIsCaught:
    """Each fact this section pins, broken in the real ``release.yml`` and rejected by
    its own check — and beside each, the spellings that mean the same thing and must
    stay green. Both directions, because a check that reddens CI on a correct workflow
    is the one that gets deleted.
    """

    @pytest.fixture(scope="module")
    @classmethod
    def release(cls):
        return RELEASE_WORKFLOW.read_text(encoding="utf-8")

    def test_renaming_the_pypi_environment_is_caught(self, release):
        broken = _mutate(release, "      name: pypi\n", "      name: pypi-prod\n")
        with pytest.raises(AssertionError, match="registered against 'pypi'"):
            check_the_publish_jobs_declare_the_registered_environments(broken)

    def test_renaming_the_testpypi_environment_is_caught(self, release):
        broken = _mutate(release, "      name: testpypi\n", "      name: test-pypi\n")
        with pytest.raises(AssertionError, match="registered against 'testpypi'"):
            check_the_publish_jobs_declare_the_registered_environments(broken)

    def test_swapping_the_two_environments_is_caught(self, release):
        broken = _mutate(release, "      name: testpypi\n", "      name: pypi\n")
        broken = _mutate(
            broken,
            "      name: pypi\n      url: https://pypi.org",
            "      name: testpypi\n      url: https://pypi.org",
        )
        with pytest.raises(AssertionError, match="registered against"):
            check_the_publish_jobs_declare_the_registered_environments(broken)

    def test_renaming_a_publish_job_is_caught(self, release):
        broken = _mutate(release, "  publish-pypi:\n", "  publish-prod:\n")
        with pytest.raises(AssertionError, match="no 'publish-pypi' job"):
            check_the_publish_jobs_declare_the_registered_environments(broken)

    def test_dropping_the_rehearsal_edge_is_caught(self, release):
        broken = _mutate(release, "    needs: publish-testpypi\n", "")
        with pytest.raises(AssertionError, match="needs None"):
            check_the_real_publish_waits_on_the_rehearsal(broken)

    def test_pointing_the_publish_straight_at_the_build_is_caught(self, release):
        broken = _mutate(release, "    needs: publish-testpypi\n", "    needs: build\n")
        with pytest.raises(AssertionError, match=r"needs \['build'\]"):
            check_the_real_publish_waits_on_the_rehearsal(broken)

    PERMISSIONS_BLOCK = "    permissions:\n      id-token: write\n"

    @pytest.mark.parametrize("job", [REHEARSAL_JOB, PUBLISH_JOB])
    def test_narrowing_a_publish_jobs_permissions_is_caught(self, release, job):
        """The likely shape of the edit: consolidating permissions and keeping a block
        that grants something else. Nothing about it reads as deleting a credential."""
        broken = _mutate_in_job(
            release, job, self.PERMISSIONS_BLOCK, "    permissions:\n      contents: read\n"
        )
        with pytest.raises(AssertionError, match="cannot mint an OIDC token"):
            check_the_publish_jobs_can_mint_an_oidc_token(broken)

    @pytest.mark.parametrize("job", [REHEARSAL_JOB, PUBLISH_JOB])
    def test_removing_a_publish_jobs_permissions_entirely_is_caught(self, release, job):
        """It then inherits the workflow's, which grants `contents: read` and no token."""
        broken = _mutate_in_job(release, job, self.PERMISSIONS_BLOCK, "")
        with pytest.raises(AssertionError, match="the workflow's"):
            check_the_publish_jobs_can_mint_an_oidc_token(broken)

    @pytest.mark.parametrize("job", [REHEARSAL_JOB, PUBLISH_JOB])
    def test_the_write_all_shorthand_is_accepted(self, release, job):
        """The false-alarm direction. `write-all` grants everything, `id-token` included,
        so calling it a failure would redden CI over a workflow that publishes fine."""
        rewritten = _mutate_in_job(
            release, job, self.PERMISSIONS_BLOCK, "    permissions: write-all\n"
        )
        check_the_publish_jobs_can_mint_an_oidc_token(rewritten)

    @pytest.mark.parametrize("job", [REHEARSAL_JOB, PUBLISH_JOB])
    def test_the_read_all_shorthand_is_caught(self, release, job):
        broken = _mutate_in_job(release, job, self.PERMISSIONS_BLOCK, "    permissions: read-all\n")
        with pytest.raises(AssertionError, match="grants nothing writable"):
            check_the_publish_jobs_can_mint_an_oidc_token(broken)

    @pytest.mark.parametrize(
        "permissions",
        [
            "    permissions: ${{ inputs.perms }}\n",
            "    permissions:\n      id-token: ${{ inputs.perms }}\n",
        ],
        ids=["shorthand", "mapping-value"],
    )
    @pytest.mark.parametrize("job", PUBLISH_JOBS)
    def test_something_it_cannot_resolve_says_so_rather_than_denying(
        self, release, job, permissions
    ):
        """ "Unresolved" and "denied" are different edits, and only one of them is a
        workflow that cannot publish. Both branches of the reader take that reading —
        pinning only the shorthand would leave the mapping free to assert a denial it
        never established.
        """
        broken = _mutate_in_job(release, job, self.PERMISSIONS_BLOCK, permissions)
        with pytest.raises(AssertionError, match="cannot tell whether"):
            check_the_publish_jobs_can_mint_an_oidc_token(broken)

    def test_hoisting_the_grant_to_the_workflow_is_accepted(self, release):
        """Actions falls back to the workflow's `permissions:` for a job declaring none,
        so this publishes exactly as before — and reading only the job would call it
        broken."""
        rewritten = release
        for job in (REHEARSAL_JOB, PUBLISH_JOB):
            rewritten = _mutate_in_job(rewritten, job, self.PERMISSIONS_BLOCK, "")
        rewritten = _mutate(
            rewritten,
            "permissions:\n  contents: read\n",
            "permissions:\n  contents: read\n  id-token: write\n",
        )
        check_the_publish_jobs_can_mint_an_oidc_token(rewritten)

    def test_dropping_the_rehearsals_repository_url_is_caught(self, release):
        """The worst of the four: the rehearsal then publishes to real PyPI, under an
        environment with no protection rules, before the `pypi` gate is actuated."""
        broken = _mutate_in_job(
            release,
            REHEARSAL_JOB,
            f"        with:\n          repository-url: {TESTPYPI_REPOSITORY_URL}\n",
            "",
        )
        with pytest.raises(AssertionError, match="uploads to"):
            check_the_rehearsal_publishes_to_the_rehearsal_index(broken)

        # Every other release check passes on it — all of them, not a sample, so this
        # stays an enumeration rather than an illustration.
        check_the_publish_jobs_declare_the_registered_environments(broken)
        check_the_real_publish_waits_on_the_rehearsal(broken)
        check_the_publish_jobs_can_mint_an_oidc_token(broken)
        check_no_release_job_continues_on_error(broken)

    def test_pointing_the_real_publish_at_an_index_of_its_own_is_caught(self, release):
        """Its *absence* is what selects pypi.org, so anything here aims the real publish
        somewhere nobody reading the job would expect."""
        broken = _mutate_in_job(
            release,
            PUBLISH_JOB,
            _publish_step_line(release),
            _publish_step_line(release)
            + "        with:\n          repository-url: https://example.invalid/legacy/\n",
        )
        with pytest.raises(AssertionError, match="names repository-url"):
            check_the_rehearsal_publishes_to_the_rehearsal_index(broken)

    @pytest.mark.parametrize("job", ["build", REHEARSAL_JOB, PUBLISH_JOB])
    def test_a_release_job_that_continues_on_error_is_caught(self, release, job):
        broken = _mutate_in_job(
            release,
            job,
            "    runs-on: ubuntu-latest\n",
            "    runs-on: ubuntu-latest\n    continue-on-error: true\n",
        )
        with pytest.raises(AssertionError, match=f"{job} \\(column {JOB_LEVEL}\\)"):
            check_no_release_job_continues_on_error(broken)
        # The rehearsal edge is still declared, and now worth nothing.
        check_the_real_publish_waits_on_the_rehearsal(broken)

    def test_aiming_the_real_publish_with_the_underscored_alias_is_caught(self, release):
        """``pypa/gh-action-pypi-publish`` honours ``repository_url`` too, so reading
        only the hyphen would leave the real publish aimable at anything, green."""
        broken = _mutate_in_job(
            release,
            PUBLISH_JOB,
            _publish_step_line(release),
            _publish_step_line(release)
            + "        with:\n          repository_url: https://example.invalid/legacy/\n",
        )
        with pytest.raises(AssertionError, match="names repository-url"):
            check_the_rehearsal_publishes_to_the_rehearsal_index(broken)

    @pytest.mark.parametrize("job", [REHEARSAL_JOB, PUBLISH_JOB])
    def test_renaming_a_publish_job_is_caught_by_the_oidc_check_too(self, release, job):
        """An absent job and a job with no ``permissions:`` are the same empty read, and
        only one of them is a workflow that publishes."""
        broken = _mutate(release, f"  {job}:\n", f"  {job}-renamed:\n")
        with pytest.raises(AssertionError, match=f"no '{job}' job"):
            check_the_publish_jobs_can_mint_an_oidc_token(broken)

    @pytest.mark.parametrize("job", ["build", REHEARSAL_JOB, PUBLISH_JOB])
    def test_a_release_step_that_continues_on_error_is_caught(self, release, job):
        """The column-blind half, and release.yml is where it belongs: a step allowed to
        fail quietly neuters whichever guard, build or publish it was wrapping. ci.yml
        cannot take this reading — its advisory actionlint pass is a legitimate use."""
        broken = _mutate_in_job(
            release,
            job,
            "    steps:\n",
            "    steps:\n      - continue-on-error: true\n        run: echo probe\n",
        )
        with pytest.raises(AssertionError, match=f"{job} \\(column 8\\)"):
            check_no_release_job_continues_on_error(broken)


# ---------------------------------------------------------------------------------------
# #336, #338: CI tests exactly the Python versions the package declares, and pip installs
# on no older one
# ---------------------------------------------------------------------------------------

# ``pyproject.toml``'s ``Programming Language :: Python :: 3.N`` classifiers and the
# ``test`` job's ``python-version`` matrix are two hand-kept copies of one list, and every
# version change edits both. Update one and not the other and CI stays green while the
# package claims a version nothing tests, or tests one it never declares. They had already
# drifted once: 3.15 was released on 2026-10-09 and was in neither until #334. A matrix is
# only half of it, too: the job's setup step must hand each leg its version, or every leg
# runs one interpreter under the matrix's names.
#
# ``requires-python``'s floor is a third copy of the list's oldest entry (#338). Leave it
# behind when that version is dropped and pip keeps installing the SDK on a Python nothing
# declares or tests; raise it early and pip refuses one that both lists still name. Only
# the floor is compared: an upper bound is deliberately absent, because a new Python should
# install before it is declared.
#
# The classifiers are read with ``tomllib``, standard library from 3.11. The 3.10 leg skips
# the comparison rather than add ``tomli`` for one test ("every dependency is debt"): the
# two files are identical on every leg, so the other legs read them for it. Everything
# that reads ``ci.yml`` alone runs on every leg, 3.10 included.

PYPROJECT = WORKFLOWS.parent.parent / "pyproject.toml"


def _toml(text: str) -> dict:
    """``text`` parsed, or the test skipped on 3.10, where ``tomllib`` is not standard."""
    tomllib = pytest.importorskip("tomllib", reason="tomllib is standard library from 3.11")
    return tomllib.loads(text)


TEST_JOB = "test"

#: A version classifier, matched whole: ``Programming Language :: Python :: 3`` and
#: ``… :: 3 :: Only`` name no version CI could test, so they are not part of the list.
VERSION_CLASSIFIER = re.compile(r"Programming Language :: Python :: (?P<version>3\.\d+)")

#: One quoted matrix entry. Quoted, because YAML reads a bare ``3.10`` as the float
#: ``3.1``: the text would still say 3.10 while CI set up 3.1, and a check that read the
#: text would vouch for it.
QUOTED_VERSION = re.compile(r"""^(?P<quote>["'])(?P<version>[^"']+)(?P=quote)$""")

#: What the setup step's ``python-version:`` must say, spaces removed before comparing.
MATRIX_VERSION_EXPRESSION = "${{matrix.python-version}}"


def declared_python_versions(pyproject_text: str) -> set[str]:
    """The ``3.N`` versions ``pyproject.toml``'s classifiers declare."""
    classifiers = _toml(pyproject_text)["project"]["classifiers"]
    return {m.group("version") for c in classifiers if (m := VERSION_CLASSIFIER.fullmatch(c))}


def _block(lines: list[str], key: str, level: int) -> list[str]:
    """The lines nested under ``key:`` at exactly ``level``, or ``[]`` when it is absent.

    The block ends where a structural line dedents back to ``level``. A comment or blank
    line can sit at any column, so neither ends it.
    """
    for index, line in enumerate(lines):
        match = KEY_LINE.match(line)
        if match and match.group("key") == key and _key_indent(line) == level:
            block: list[str] = []
            for nested in lines[index + 1 :]:
                if _is_structural(nested) and _key_indent(nested) <= level:
                    break
                block.append(nested)
            return block
    return []


def _test_job_body(ci_text: str) -> list[str]:
    body = job_bodies(ci_text).get(TEST_JOB)
    assert body is not None, f"ci.yml has no {TEST_JOB!r} job — the matrix moved"
    return body


def matrix_python_versions(ci_text: str) -> list[str]:
    """The ``test`` job's ``strategy.matrix.python-version`` entries, as written.

    Scoped to the job's own ``strategy:`` block, so a ``python-version:`` anywhere else
    (the setup step's ``with:`` passes one on) is never read as the matrix. A shape this
    reader cannot see through fails with the reason rather than reading as some list:
    ``include:``/``exclude:`` can add or remove versions the inline list does not show, an
    expression is only known at run time, and an unquoted entry may not be the version it
    reads as.
    """
    strategy = _block(_test_job_body(ci_text), "strategy", JOB_LEVEL)
    matrix = level_mapping(strategy, "matrix", JOB_LEVEL + 2)
    assert matrix is not None, (
        f"the {TEST_JOB!r} job has no strategy.matrix mapping this check can read. It "
        f"reads one written in block style with every value on its key's line: no "
        f"expression for the whole matrix, and no list written as - entries beneath a key "
        f'(python-version: ["3.10", "3.11"], never python-version: then - "3.10")'
    )

    rewriters = sorted({"include", "exclude"} & matrix.keys())
    assert not rewriters, (
        f"the {TEST_JOB!r} matrix has {rewriters}, which can change the versions CI runs "
        f"without touching the python-version list this check reads. Keep the versions in "
        f"that one inline list, or teach this check to read the new shape."
    )
    value = matrix.get("python-version", "")
    assert value.startswith("[") and value.endswith("]"), (
        f"the {TEST_JOB!r} matrix's python-version is {value!r}; this check reads only a "
        f'one-line inline list, written python-version: ["3.10", "3.11"]'
    )

    versions: list[str] = []
    for entry in (entry.strip() for entry in value[1:-1].split(",") if entry.strip()):
        quoted = QUOTED_VERSION.match(entry)
        assert quoted, (
            f"matrix entry {entry} is unquoted. YAML reads a bare 3.10 as the float 3.1, "
            f'so CI would set up a version the text does not say. Quote it: "{entry}"'
        )
        versions.append(quoted.group("version"))

    repeated = sorted({version for version in versions if versions.count(version) > 1})
    assert not repeated, (
        f"the {TEST_JOB!r} matrix lists {repeated} more than once: a copy-paste slip, and "
        f"most likely a different version was meant"
    )
    return versions


def check_each_leg_sets_up_its_matrix_version(ci_text: str) -> None:
    """The ``test`` job's steps pass ``matrix.python-version`` on, once, and nothing else.

    Read from the ``steps:`` block alone, so the matrix's own ``python-version:`` is not
    counted. Without it, setup-uv falls back to ``.python-version`` and every leg tests
    that one interpreter while the matrix and the classifiers agree perfectly.
    """
    steps = _block(_test_job_body(ci_text), "steps", JOB_LEVEL)
    values = [value.strip("\"'").replace(" ", "") for value in step_values(steps, "python-version")]
    assert values == [MATRIX_VERSION_EXPRESSION], (
        f"the {TEST_JOB!r} job's steps must set up ${{{{ matrix.python-version }}}} exactly "
        f"once, as the setup step's python-version input; they set {values}"
    )


def check_ci_tests_the_declared_python_versions(ci_text: str, pyproject_text: str) -> None:
    # The matrix first: its shape checks need no TOML, so they still run on the 3.10 leg,
    # where reading the classifiers skips.
    tested = set(matrix_python_versions(ci_text))
    declared = declared_python_versions(pyproject_text)
    assert declared, "pyproject.toml declares no Programming Language :: Python :: 3.N"

    untested = sorted(declared - tested)
    undeclared = sorted(tested - declared)
    assert not untested and not undeclared, (
        f"pyproject.toml's classifiers and ci.yml's {TEST_JOB!r} matrix must name the same "
        f"Python versions. Declared but never tested: {untested}. Tested but never "
        f"declared: {undeclared}. A version change edits both lists."
    )


def test_ci_tests_exactly_the_declared_python_versions():
    check_ci_tests_the_declared_python_versions(
        CI_WORKFLOW.read_text(encoding="utf-8"), PYPROJECT.read_text(encoding="utf-8")
    )


def test_each_test_leg_sets_up_its_matrix_version():
    check_each_leg_sets_up_its_matrix_version(CI_WORKFLOW.read_text(encoding="utf-8"))


#: The one ``requires-python`` form the floor check reads: a lone lower bound.
REQUIRES_PYTHON_FLOOR = re.compile(r">=(?P<version>3\.\d+)")

#: ``pyproject.toml``'s ``requires-python`` line, found rather than pinned, for the probes.
REQUIRES_PYTHON_LINE = re.compile(r"^requires-python = .*\n", re.MULTILINE)


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric order, so 3.9 sorts before 3.10 (as text it would sort after)."""
    return tuple(int(part) for part in version.split("."))


def oldest_declared_python_version(pyproject_text: str) -> str:
    """The oldest ``3.N`` the classifiers declare, ordered as numbers."""
    declared = declared_python_versions(pyproject_text)
    assert declared, "pyproject.toml declares no Programming Language :: Python :: 3.N"
    return min(declared, key=_version_key)


def check_requires_python_floor_is_the_oldest_declared(pyproject_text: str) -> None:
    requires = _toml(pyproject_text)["project"].get("requires-python")
    assert requires is not None, (
        "pyproject.toml has no requires-python, so pip installs the SDK on any Python; "
        'it must be ">=" followed by the oldest declared version'
    )
    floor = REQUIRES_PYTHON_FLOOR.fullmatch(requires)
    assert floor, (
        f'requires-python is {requires!r}; this check reads only a lone floor, ">=3.N" '
        f"with no space, no patch number, and no other operator or clause. An upper bound is "
        f"deliberately absent, so a new Python installs before it is declared."
    )
    oldest = oldest_declared_python_version(pyproject_text)
    assert floor.group("version") == oldest, (
        f"requires-python is {requires!r} but the oldest declared Python is {oldest}: "
        f'it must be ">={oldest}". Lower, and pip installs the SDK on a Python nothing '
        f"declares or tests; higher, and pip refuses one both lists still name. Dropping "
        f"a version edits every file that names it (git grep -nF for the version being "
        f"dropped finds them); regenerate uv.lock with uv lock rather than editing it."
    )


def test_requires_python_floor_is_the_oldest_declared_version():
    check_requires_python_floor_is_the_oldest_declared(PYPROJECT.read_text(encoding="utf-8"))


#: The test job's matrix line, found rather than pinned, so the probes below survive the
#: very version changes the check above polices.
MATRIX_LINE = re.compile(r"^ {8}python-version: \[.*\]\n", re.MULTILINE)

#: A version no classifier or matrix will name in this SDK's lifetime: the probes' drift.
PROBE_VERSION = "3.99"


class TestEachPythonVersionDriftIsCaught:
    """One probe per way the two lists, or the matrix and its consumer, can come apart.

    Each probe edits the real files, and finds what it edits rather than naming today's
    versions, so dropping 3.10 or adding 3.16 changes no probe.
    """

    @pytest.fixture(scope="module")
    @classmethod
    def ci(cls):
        return CI_WORKFLOW.read_text(encoding="utf-8")

    @pytest.fixture(scope="module")
    @classmethod
    def pyproject(cls):
        return PYPROJECT.read_text(encoding="utf-8")

    @pytest.fixture(scope="module")
    @classmethod
    def matrix_line(cls, ci):
        lines = MATRIX_LINE.findall(ci)
        assert len(lines) == 1, f"expected one matrix python-version line, found {lines}"
        return lines[0]

    @pytest.fixture(scope="module")
    @classmethod
    def oldest(cls, ci):
        """The first version the matrix lists — the one the probes drop or unquote."""
        return matrix_python_versions(ci)[0]

    def _classifier(self, version: str) -> str:
        return f'    "Programming Language :: Python :: {version}",\n'

    def _with_matrix(self, ci: str, matrix_line: str, replacement: str) -> str:
        return _mutate_in_job(ci, TEST_JOB, matrix_line, replacement)

    def test_a_version_added_to_the_matrix_alone_is_caught(self, ci, pyproject, matrix_line):
        broken = self._with_matrix(
            ci, matrix_line, matrix_line.replace("]\n", f', "{PROBE_VERSION}"]\n')
        )
        with pytest.raises(
            AssertionError, match=rf"never declared: \['{re.escape(PROBE_VERSION)}'\]"
        ):
            check_ci_tests_the_declared_python_versions(broken, pyproject)

    def test_a_version_added_to_the_classifiers_alone_is_caught(self, ci, pyproject, oldest):
        anchor = self._classifier(oldest)
        broken = _mutate(pyproject, anchor, anchor + self._classifier(PROBE_VERSION))
        with pytest.raises(
            AssertionError, match=rf"never tested: \['{re.escape(PROBE_VERSION)}'\]"
        ):
            check_ci_tests_the_declared_python_versions(ci, broken)

    def test_a_version_dropped_from_the_matrix_alone_is_caught(
        self, ci, pyproject, matrix_line, oldest
    ):
        broken = self._with_matrix(ci, matrix_line, matrix_line.replace(f'"{oldest}", ', ""))
        with pytest.raises(AssertionError, match=rf"never tested: \['{re.escape(oldest)}'\]"):
            check_ci_tests_the_declared_python_versions(broken, pyproject)

    def test_a_version_dropped_from_the_classifiers_alone_is_caught(self, ci, pyproject, oldest):
        broken = _mutate(pyproject, self._classifier(oldest), "")
        with pytest.raises(AssertionError, match=rf"never declared: \['{re.escape(oldest)}'\]"):
            check_ci_tests_the_declared_python_versions(ci, broken)

    def test_an_unquoted_matrix_entry_is_refused(self, ci, pyproject, matrix_line, oldest):
        broken = self._with_matrix(ci, matrix_line, matrix_line.replace(f'"{oldest}"', oldest))
        with pytest.raises(AssertionError, match="unquoted"):
            check_ci_tests_the_declared_python_versions(broken, pyproject)

    def test_a_repeated_matrix_entry_is_refused(self, ci, pyproject, matrix_line, oldest):
        broken = self._with_matrix(
            ci, matrix_line, matrix_line.replace(f'"{oldest}"', f'"{oldest}", "{oldest}"')
        )
        with pytest.raises(AssertionError, match="more than once"):
            check_ci_tests_the_declared_python_versions(broken, pyproject)

    @pytest.mark.parametrize("rewriter", ["include", "exclude"])
    @pytest.mark.parametrize(
        ("spelling", "expected_error"),
        [
            ('{rewriter}:\n          - python-version: "{probe}"\n', "no strategy.matrix"),
            ('{rewriter}: [{{python-version: "{probe}"}}]\n', r"\['{rewriter}'\]"),
        ],
        ids=["block-list", "inline"],
    )
    def test_a_matrix_include_or_exclude_is_refused(
        self, ci, pyproject, matrix_line, rewriter, spelling, expected_error
    ):
        added = "        " + spelling.format(rewriter=rewriter, probe=PROBE_VERSION)
        broken = self._with_matrix(ci, matrix_line, matrix_line + added)
        with pytest.raises(AssertionError, match=expected_error.format(rewriter=rewriter)):
            check_ci_tests_the_declared_python_versions(broken, pyproject)

    def test_a_block_list_python_version_is_refused(self, ci, pyproject, matrix_line, oldest):
        broken = self._with_matrix(
            ci, matrix_line, f'        python-version:\n          - "{oldest}"\n'
        )
        with pytest.raises(AssertionError, match="no strategy.matrix"):
            check_ci_tests_the_declared_python_versions(broken, pyproject)

    def test_a_matrix_from_an_expression_is_refused(self, ci, pyproject, matrix_line):
        broken = self._with_matrix(
            ci, matrix_line, "        python-version: ${{ fromJSON(vars.PYTHONS) }}\n"
        )
        with pytest.raises(AssertionError, match="one-line inline list"):
            check_ci_tests_the_declared_python_versions(broken, pyproject)

    def test_the_setup_steps_python_version_is_not_read_as_the_matrix(
        self, ci, pyproject, matrix_line
    ):
        """Delete the matrix's list and the step's ``with: python-version:`` is the only
        one left in the job; reading it would compare the classifiers to an expression."""
        broken = self._with_matrix(ci, matrix_line, "        os: [ubuntu-latest]\n")
        with pytest.raises(AssertionError, match="python-version is ''"):
            check_ci_tests_the_declared_python_versions(broken, pyproject)

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            # A pinned version: one interpreter on every leg.
            ("python-version: ${{ matrix.python-version }}", 'python-version: "3.12"'),
            # No input at all: setup-uv falls back to .python-version. The matrix's own
            # python-version: sits at a step's depth, so this is also the probe that it
            # is never counted as the setup.
            # The input alone, not its `with:`: the step reads its uv from
            # uv.lock through the same block (#341, #346).
            ("          python-version: ${{ matrix.python-version }}\n", ""),
        ],
        ids=["pinned", "removed"],
    )
    def test_a_setup_step_that_ignores_the_matrix_is_caught(self, ci, old, new):
        broken = _mutate_in_job(ci, TEST_JOB, old, new)
        with pytest.raises(AssertionError, match="exactly once"):
            check_each_leg_sets_up_its_matrix_version(broken)


class TestTheRequiresPythonFloorDriftIsCaught:
    """One probe per direction the floor can leave the oldest declared version, and one
    per shape the check refuses to read. Each edits the real file's own lines, and derives
    its versions from the oldest one declared, so a version change changes no probe."""

    @pytest.fixture(scope="module")
    @classmethod
    def pyproject(cls):
        return PYPROJECT.read_text(encoding="utf-8")

    @pytest.fixture(scope="module")
    @classmethod
    def floor_line(cls, pyproject):
        lines = REQUIRES_PYTHON_LINE.findall(pyproject)
        assert len(lines) == 1, f"expected one requires-python line, found {lines}"
        return lines[0]

    @pytest.fixture(scope="module")
    @classmethod
    def oldest_minor(cls, pyproject):
        """The oldest declared version's minor number: 10 for 3.10."""
        return _version_key(oldest_declared_python_version(pyproject))[1]

    def _requires(self, floor: str) -> str:
        return f'requires-python = "{floor}"\n'

    def test_a_floor_below_the_oldest_declared_version_is_caught(
        self, pyproject, floor_line, oldest_minor
    ):
        """The state dropping the oldest version and forgetting the floor leaves behind."""
        broken = _mutate(pyproject, floor_line, self._requires(f">=3.{oldest_minor - 1}"))
        with pytest.raises(AssertionError, match=rf"oldest declared Python is 3\.{oldest_minor}:"):
            check_requires_python_floor_is_the_oldest_declared(broken)

    def test_a_floor_above_the_oldest_declared_version_is_caught(
        self, pyproject, floor_line, oldest_minor
    ):
        broken = _mutate(pyproject, floor_line, self._requires(f">=3.{oldest_minor + 1}"))
        with pytest.raises(AssertionError, match=rf"oldest declared Python is 3\.{oldest_minor}:"):
            check_requires_python_floor_is_the_oldest_declared(broken)

    def test_versions_are_ordered_as_numbers_not_text(self, pyproject):
        """As text, 3.9 sorts after every 3.1N, so a text ``min`` would miss it."""
        anchor = '    "Programming Language :: Python :: 3",\n'
        added = '    "Programming Language :: Python :: 3.9",\n'
        broken = _mutate(pyproject, anchor, anchor + added)
        with pytest.raises(AssertionError, match=r"oldest declared Python is 3\.9:"):
            check_requires_python_floor_is_the_oldest_declared(broken)

    def test_a_missing_floor_is_caught(self, pyproject, floor_line):
        broken = _mutate(pyproject, floor_line, "")
        with pytest.raises(AssertionError, match="has no requires-python"):
            check_requires_python_floor_is_the_oldest_declared(broken)

    @pytest.mark.parametrize("form", ["~=3.{m}", ">=3.{m},<4", ">= 3.{m}", ">=3.{m}.0"])
    def test_a_floor_in_any_other_form_is_refused(self, pyproject, floor_line, oldest_minor, form):
        broken = _mutate(pyproject, floor_line, self._requires(form.format(m=oldest_minor)))
        with pytest.raises(AssertionError, match="reads only a lone floor"):
            check_requires_python_floor_is_the_oldest_declared(broken)


# The sixth invariant (#339): wherever a workflow holds power, its actions are pinned to a
# commit SHA. `release.yml` ran `pypa/gh-action-pypi-publish@release/v1` -- a branch -- in
# the two jobs that hold `id-token: write`, so whatever its owner (or whoever took over its
# owner's account) pushed there would run on our next tag with the power to publish this
# package under our name. A tag moves the same way; only a full commit SHA does not.
#
# The scope is a workflow, not a job, and it is decided by reading the file rather than by
# a list that has to be remembered: any secret, any write grant, or no top-level
# `permissions:` at all (which inherits the repository's default, possibly write) puts
# every action in the file under the rule. Whole-file on purpose: `release.yml`'s build
# job holds nothing, but it makes the bytes the publish jobs upload. A workflow that holds
# nothing -- `ci.yml` today -- is left alone; the day one of its jobs gains a secret, this
# fails until its actions are pinned.
#
# The capital's shared stubs are out of scope: they call reusable workflows in
# `basecradle/.github`, editing them here trips the NOC's drift-guard, and which files they
# are is CLAUDE.md -> Propagation's to say -- read from there, the way ci.yml's actionlint
# job reads it.

CLAUDE_MD = WORKFLOWS.parent.parent / "CLAUDE.md"
PROPAGATION_HEADING = "### Propagation"
WORKFLOW_PATH = re.compile(r"\.github/workflows/(?P<name>[a-z-]+\.ya?ml)")

#: ``owner/repo`` (and, for an action in a subdirectory or a reusable workflow, a path),
#: then ``@`` and exactly forty lowercase hex digits. Seven is a short SHA, which GitHub
#: refuses for actions; anything else is a tag or a branch, which moves.
SHA_PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[^@\s]+)?@[0-9a-f]{40}$")

#: The version, after the SHA, in the comment Dependabot reads and rewrites with it. A
#: bare SHA works, but nobody reviewing a bump can tell what it moved from or to. The
#: ``v`` is optional because the comment is the action's own tag name, whatever it is.
VERSION_COMMENT = re.compile(r"^#\s*v?\d+(?:\.\d+)*(?:\s|$)")

#: A container image by digest -- the other reference that cannot move.
DIGEST_PINNED = re.compile(r"^docker://\S+@sha256:[0-9a-f]{64}$")

#: Code in this repository, at the commit being run: nothing to pin.
LOCAL_ACTION_PREFIX = "./"

#: A secret read by name or index, or every secret handed on to a reusable workflow.
SECRET_REFERENCE = re.compile(r"\bsecrets\s*(?:\.|\[|:\s*inherit\b)")

TOP_LEVEL_PERMISSIONS = re.compile(r"^permissions:")


def shared_stub_names(claude_md: str) -> set[str]:
    """The workflow files CLAUDE.md -> Propagation names as the capital's.

    The paragraph is found by its heading and read as the first non-empty line after it,
    exactly as ci.yml's actionlint job reads it, so the two cannot disagree about which
    files are shared.

    Empty, never an exception, when the heading or its paragraph is gone: this runs at
    import, where a raise would stop every invariant in the file from being collected.
    An empty exclusion puts the stubs in scope, and
    ``test_the_shared_stubs_are_read_from_claude_md`` says why.
    """
    lines = claude_md.splitlines()
    if PROPAGATION_HEADING not in lines:
        return set()
    start = lines.index(PROPAGATION_HEADING) + 1
    paragraph = next((line for line in lines[start:] if line.strip()), "")
    return {match.group("name") for match in WORKFLOW_PATH.finditer(paragraph)}


def owned_workflows() -> list[Path]:
    """Every workflow in this repository that is this repository's to edit.

    A missing CLAUDE.md reads as no exclusions, for ``shared_stub_names``' reason: this
    runs at import.
    """
    claude_md = CLAUDE_MD.read_text(encoding="utf-8") if CLAUDE_MD.is_file() else ""
    shared = shared_stub_names(claude_md)
    return sorted(path for path in WORKFLOWS.glob("*.y*ml") if path.name not in shared)


def privilege_reasons(text: str) -> list[str]:
    """Why a workflow holds power -- empty if it holds none this reader can see.

    It errs toward finding power: a false positive costs a pinned action, a false
    negative an unpinned one in a job that can publish. So a secret is looked for in
    every line but a YAML comment: inside a ``run:`` script a ``#`` line is shell, and
    Actions substitutes ``${{ secrets.X }}`` into it before the shell ever sees the
    ``#`` -- commented-out or not, the secret is in the script. A trailing YAML comment
    that mentions one counts too. And any ``permissions:`` value naming ``write`` --
    ``write-all``, or a flow mapping like ``{ id-token: write }`` that this module's
    readers do not otherwise parse -- is a grant.
    """
    reasons = []
    lines = text.splitlines()
    if not any(TOP_LEVEL_PERMISSIONS.match(line) for line in lines):
        reasons.append("no top-level permissions: it inherits the repository's default")

    inside_a_script = block_scalar_line_numbers(lines)
    for index, line in enumerate(lines):
        if index not in inside_a_script and line.lstrip().startswith("#"):
            continue
        if SECRET_REFERENCE.search(line):
            reasons.append(f"line {index + 1} reads a secret: {line.strip()}")

    for index, line in enumerate(lines):
        if index in inside_a_script or not (match := KEY_LINE.match(line)):
            continue
        value = _strip_comment(match.group("value") or "").strip("\"'")
        if value == "write" or (match.group("key") == "permissions" and "write" in value):
            reasons.append(f"line {index + 1} grants write: {line.strip()}")

    return reasons


class UsesReference(NamedTuple):
    """One ``uses:`` in a workflow: where it is, what it names, and its trailing comment."""

    index: int  # 0-indexed line
    line: str
    action: str  # the reference, quotes dropped
    comment: str  # the trailing YAML comment, ``#`` included, or ""


def uses_references(lines: list[str]) -> list[UsesReference]:
    """Every ``uses:`` in a workflow's lines -- the one reader of them in this module.

    Steps and job-level reusable-workflow calls alike, both of which run someone's code
    with the job's power. ``run:`` scripts are skipped, as every reader here skips them:
    a ``uses:`` in shell is text, not an action. A ``uses:`` this cannot read at all is
    ``unreadable_key_lines``' to report. The pin rule, the toolchain rule and the live
    pin-comment check all read through here, so they cannot disagree about which actions
    a workflow uses.
    """
    inside_a_script = block_scalar_line_numbers(lines)
    references = []
    for index, line in enumerate(lines):
        match = KEY_LINE.match(line)
        if index in inside_a_script or not match or match.group("key") != "uses":
            continue
        raw = match.group("value") or ""
        reference = _strip_comment(raw)
        comment = raw[len(reference) :].strip()
        references.append(UsesReference(index, line, reference.strip("\"'"), comment))
    return references


def unpinned_actions(name: str, text: str) -> list[str]:
    """Every ``uses:`` in a workflow that is not a SHA pin carrying its version comment."""
    unpinned = []
    for ref in uses_references(text.splitlines()):
        if not ref.action or ref.action.startswith(LOCAL_ACTION_PREFIX):
            continue
        where = f"{name}:{ref.index + 1}: {ref.line.strip()}"
        if not (SHA_PINNED.match(ref.action) or DIGEST_PINNED.match(ref.action)):
            unpinned.append(
                f"{where} -- not pinned to a full 40-character commit SHA "
                f"(or, for a docker:// image, a sha256 digest)"
            )
        elif not VERSION_COMMENT.match(ref.comment):
            unpinned.append(f"{where} -- pinned, but no `# vX.Y.Z` comment names the version")
    return unpinned


def check_privileged_actions_are_pinned(name: str, text: str) -> None:
    """Assert a workflow holding a secret or a write grant runs only SHA-pinned actions."""
    reasons = privilege_reasons(text)
    if not reasons:
        return
    unpinned = unpinned_actions(name, text)
    assert not unpinned, (
        f"{name} holds power ({'; '.join(reasons)}), so every action in it must "
        f"be pinned to a full commit SHA with its version in a trailing comment -- a tag "
        f"or a branch runs whatever its owner pushes there next, with this workflow's "
        f"secrets and grants (#339). Read the SHA off the action's own repository "
        f"(`git ls-remote https://github.com/OWNER/REPO 'refs/tags/vX.Y.Z^{{}}'`), never "
        f"from memory or another workflow:\n" + "\n".join(unpinned)
    )


OWNED_WORKFLOWS = owned_workflows()


def test_the_shared_stubs_are_read_from_claude_md():
    """The exclusion list is real files, or a renamed heading would exclude nothing."""
    shared = shared_stub_names(CLAUDE_MD.read_text(encoding="utf-8"))
    assert shared, f"no workflow paths under {PROPAGATION_HEADING!r} in CLAUDE.md"
    assert all((WORKFLOWS / name).is_file() for name in shared), shared


@pytest.mark.parametrize("name", ["release.yml", "dependency-security-scan.yml"])
def test_the_workflows_that_hold_power_are_in_scope(name):
    """Both halves, or the pin check passes on air: the file must be one this test
    checks (not read as a shared stub), and the privilege reader must see its power."""
    workflow = WORKFLOWS / name
    assert workflow in OWNED_WORKFLOWS, f"{name} is excluded as a shared stub"
    assert privilege_reasons(workflow.read_text(encoding="utf-8")), name


@pytest.mark.parametrize("workflow", OWNED_WORKFLOWS, ids=lambda path: path.name)
def test_a_workflow_that_holds_power_pins_every_action(workflow):
    check_privileged_actions_are_pinned(workflow.name, workflow.read_text(encoding="utf-8"))


@pytest.mark.parametrize("workflow", OWNED_WORKFLOWS, ids=lambda path: path.name)
def test_no_uses_escapes_the_pin_scanner(workflow):
    unreadable = unreadable_key_lines(workflow, "uses")
    assert not unreadable, (
        "a `uses:` the pin scanner cannot read is one it cannot hold to a SHA -- write it "
        "as a plain `uses: owner/repo@sha # vX.Y.Z` on one line:\n" + "\n".join(unreadable)
    )


PINNED = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"


class TestThePinScannerItself:
    """Where the pin scanner fires and where it does not, on fabricated workflows."""

    @staticmethod
    def scan(uses_value: str) -> list[str]:
        text = (
            f"jobs:\n  a:\n    steps:\n      - uses: {uses_value}\n        with:\n          x: 1\n"
        )
        return unpinned_actions("w.yml", text)

    @pytest.mark.parametrize(
        "value",
        [
            f"{PINNED} # v7.0.1",
            f"{PINNED}  # v7.0.1 (the v7 tag on 2026-11-16)",
            f'"{PINNED}" # v7.0.1',
            f"{PINNED} # v7",
            f"{PINNED} # 7.0.1",
            "owner/repo/.github/workflows/w.yml@3d3c42e5aac5ba805825da76410c181273ba90b1 # v1",
            "owner/repo/sub/action@3d3c42e5aac5ba805825da76410c181273ba90b1 # v2.0.0",
            "./.github/actions/local",
            "docker://ghcr.io/o/tool@sha256:" + "0123456789abcdef" * 4 + " # v1.2.3",
        ],
    )
    def test_accepts(self, value):
        assert self.scan(value) == []

    @pytest.mark.parametrize(
        "value",
        [
            "actions/checkout@v7",
            "actions/checkout@v7 # v7",
            "pypa/gh-action-pypi-publish@release/v1",
            "actions/checkout@main",
            "actions/checkout@3d3c42e # v7.0.1",
            "actions/checkout@3D3C42E5AAC5BA805825DA76410C181273BA90B1 # v7.0.1",
            f"{PINNED}0 # v7.0.1",
            "actions/checkout",
            "docker://alpine:3",
            "docker://alpine@sha256:0123456789abcdef # v3",
        ],
    )
    def test_refuses_a_ref_that_moves(self, value):
        assert "not pinned" in " ".join(self.scan(value))

    @pytest.mark.parametrize("value", [PINNED, f"{PINNED} # pinned", f"{PINNED} #"])
    def test_refuses_a_pin_whose_version_is_not_named(self, value):
        assert "no `# vX.Y.Z` comment" in " ".join(self.scan(value))

    def test_a_uses_inside_a_run_script_is_not_an_action(self, tmp_path):
        workflow = tmp_path / "w.yml"
        workflow.write_text(
            "jobs:\n  a:\n    steps:\n      - run: |\n          echo 'uses: actions/x@v1'\n",
            encoding="utf-8",
        )
        assert unpinned_actions(workflow.name, workflow.read_text(encoding="utf-8")) == []
        assert unreadable_key_lines(workflow, "uses") == []

    def test_a_job_level_reusable_workflow_call_is_held_to_the_rule(self):
        text = "jobs:\n  a:\n    uses: o/r/.github/workflows/w.yml@main\n"
        assert len(unpinned_actions("w.yml", text)) == 1

    @pytest.mark.parametrize(
        "line",
        [
            "      - {uses: actions/checkout@v7}",
            "      - uses:\n          actions/checkout@v7",
        ],
    )
    def test_a_uses_it_cannot_read_is_reported(self, tmp_path, line):
        workflow = tmp_path / "w.yml"
        workflow.write_text(f"jobs:\n  a:\n    steps:\n{line}\n", encoding="utf-8")
        assert unreadable_key_lines(workflow, "uses")

    @pytest.mark.parametrize(
        "line",
        [
            "        env:\n          T: ${{ secrets.TOKEN }}",
            "        env:\n          T: ${{ secrets['TOKEN'] }}",
            "        run: echo ${{ secrets.TOKEN }}",
            '        run: |\n          curl -H "x: ${{ secrets.TOKEN }}" example.com',
            "    secrets: inherit",
            "    permissions:\n      contents: write",
            "    permissions:\n      id-token: write  # Trusted Publishing",
            "    permissions:\n      issues: 'write'",
            "    permissions: write-all",
            "    permissions: { id-token: write }",
            "    permissions: {contents: read, pull-requests: write}",
            "        run: |\n          # echo ${{ secrets.TOKEN }}",
        ],
    )
    def test_finds_the_power_a_workflow_holds(self, line):
        text = f"permissions:\n  contents: read\njobs:\n  a:\n    steps:\n      - run: x\n{line}\n"
        assert privilege_reasons(text)

    def test_a_workflow_with_no_top_level_permissions_holds_the_default(self):
        assert privilege_reasons("jobs:\n  a:\n    steps:\n      - run: x\n")

    @pytest.mark.parametrize(
        "line",
        [
            "    permissions:\n      contents: read",
            "    permissions: read-all",
            "      # the publish needs secrets.TOKEN and contents: write",
            "    permissions: { contents: read }",
            "        run: |\n          echo 'contents: write'",
        ],
    )
    def test_a_workflow_holding_nothing_is_left_alone(self, line):
        text = f"permissions:\n  contents: read\njobs:\n  a:\n    steps:\n      - run: x\n{line}\n"
        assert privilege_reasons(text) == []


class TestEachPinEditIsCaught:
    """The edits that unpin a workflow holding power, each made to the real file."""

    @pytest.fixture(scope="module")
    @classmethod
    def release(cls) -> str:
        return RELEASE_WORKFLOW.read_text(encoding="utf-8")

    @staticmethod
    def _pin(text: str, action: str) -> str:
        """The first ``uses:`` value in ``text`` naming ``action``, comment included."""
        match = re.search(rf"uses:\s+({re.escape(action)}@\S+\s+#\s*\S+)", text)
        assert match, f"no pinned {action} in the real workflow -- this test has gone stale"
        return match.group(1)

    @pytest.mark.parametrize(
        "replacement",
        [
            "pypa/gh-action-pypi-publish@release/v1",
            "pypa/gh-action-pypi-publish@v1.14.3 # v1.14.3",
            "pypa/gh-action-pypi-publish@7245906 # v1.14.3",
        ],
    )
    def test_unpinning_the_publish_action_is_caught(self, release, replacement):
        pin = self._pin(release, "pypa/gh-action-pypi-publish")
        broken = _mutate_in_job(release, PUBLISH_JOB, pin, replacement)
        with pytest.raises(AssertionError, match="gh-action-pypi-publish@"):
            check_privileged_actions_are_pinned(RELEASE_WORKFLOW.name, broken)

    def test_unpinning_the_build_jobs_checkout_is_caught(self, release):
        pin = self._pin(release, "actions/checkout")
        broken = _mutate_in_job(release, "build", pin, "actions/checkout@v7")
        with pytest.raises(AssertionError, match="actions/checkout@v7"):
            check_privileged_actions_are_pinned(RELEASE_WORKFLOW.name, broken)

    def test_dropping_a_pins_version_comment_is_caught(self, release):
        pin = self._pin(release, "pypa/gh-action-pypi-publish")
        broken = _mutate_in_job(release, PUBLISH_JOB, pin, re.split(r"\s+#", pin)[0])
        with pytest.raises(AssertionError, match="no `# vX.Y.Z` comment"):
            check_privileged_actions_are_pinned(RELEASE_WORKFLOW.name, broken)

    def test_dropping_the_top_level_permissions_is_itself_read_as_power(self, release):
        """Asserted by its own reason: release.yml's id-token grants would keep the list
        non-empty without it, and the probe would prove nothing."""
        broken = _mutate(release, "permissions:\n  contents: read\n", "")
        assert any("inherits the repository's default" in r for r in privilege_reasons(broken))

    def test_ci_gaining_a_secret_brings_it_into_scope(self):
        """A workflow holding nothing today is held to the rule the day it holds a secret.

        The probe brings its own unpinned step, so pinning ci.yml's actions -- a fine
        thing to do -- does not turn this into a test that needs rewriting.
        """
        ci = CI_WORKFLOW.read_text(encoding="utf-8")
        broken = (
            ci + "        env:\n          T: ${{ secrets.T }}\n      - uses: actions/probe@v1\n"
        )
        with pytest.raises(AssertionError, match=r"reads a secret(.|\n)*actions/probe@v1"):
            check_privileged_actions_are_pinned(CI_WORKFLOW.name, broken)


# ---------------------------------------------------------------------------------------
# The seventh invariant (#341, #346): the toolchain that builds the published bytes is held
# by uv.lock, not fetched fresh on each tag. #339 pinned which *actions* run in
# `release.yml`; it did not pin what they fetch, and whatever builds the bytes is as
# powerful as whatever uploads them. #341 pinned uv (in `.tool-versions`) and hatchling
# (exactly, in `[build-system].requires`); #346 took the shape basecradle-harness shipped
# (basecradle-harness#916) and moved every link into the lock, where Dependabot moves it as
# a reviewable diff and the daily scan (#315) reads it:
#
# - **uv** is the `build` dependency group's. The one build,
#   `.github/actions/build-distributions`, installs the very wheel the lock holds, by pip in
#   hash-checking mode from the lock's own sha256s (`scripts/locked_requirement.py`), so
#   setup-uv is off the publish path. CI's setup-uv steps read the same version off the
#   lock (`version-file: uv.lock`). Not `[tool.uv] required-version`: uv enforces that on
#   every contributor's every local command.
# - **The build backend and everything it imports.** `uv build` resolves
#   `[build-system].requires` in an isolated environment, outside uv.lock. Every name there
#   is declared in the `build` group with the same specifier, so the lock holds it and its
#   whole closure, and the build exports them as hashed constraints and runs
#   `--require-hashes`, which refuses a build requirement with no trusted hash. The table
#   itself is a range again: it ships in the sdist, and an exact pin there bound anyone
#   rebuilding the sdist without build isolation.
# - **The interpreter** is uv-managed (`--managed-python`), never the runner image's.
# - **One build.** The release and CI's `build` job both run the action, and the gate
#   needs that job, so a toolchain bump is built on its pull request before it can build a
#   release. Nothing else in a workflow builds.
# - **The checkout's token.** actions/checkout leaves `GITHUB_TOKEN` in `.git/config`
#   unless told not to, while the build runs the backend's code in that tree. Every
#   checkout in a workflow that holds power -- the pin rule's scope -- says
#   `persist-credentials: false`.
#
# And one gap in #339's guard: it checked that each pin *carries* a `# vX.Y.Z` comment,
# not that the comment *names* the pinned commit. A comment that lies tells a reviewer of
# the next Dependabot bump the wrong starting point. That check needs the network, so it
# is marked `live` and runs in the drift-guard job.

LOCK = WORKFLOWS.parent.parent / "uv.lock"
UV_VERSION_FILE = LOCK.name
BUILD_ACTION = WORKFLOWS.parent / "actions" / "build-distributions" / "action.yml"
#: How a workflow names the build action in its ``uses:``.
BUILD_ACTION_REF = "./.github/actions/build-distributions"
SETUP_UV = "astral-sh/setup-uv"
CHECKOUT = "actions/checkout"

#: A version setup-uv installs as exactly itself: no range, no ``latest``, no ``v``.
EXACT_UV_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

#: A requirement's name: what precedes its specifier, extras or marker.
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

#: Anything that builds a distribution. A build outside the action is one the lock's
#: constraints do not reach.
BUILDER = re.compile(r"\buv build\b|-m build\b|\bpyproject-build\b|\bpip wheel\b|\bhatch build\b")

#: Anything that installs or fetches a tool. On the publish path the one install allowed is
#: the uv bootstrap: pip, by the lock's hashes, no dependencies, no source builds.
INSTALLER = re.compile(
    r"\b(?:pip3? (?:install|download)|pipx|uv (?:tool |pip |python )?install|uv pip\b|uvx"
    r"|uv run\b[^;&|\n]*--with|ensurepip|conda|mamba|curl|wget|apt(?:-get)?|npm|npx"
    r"|gh release download)\b"
)
LOCKED_PIP = ("--require-hashes", "--no-deps", "--only-binary :all:")

#: The three commands the build's integrity rests on, exactly as the build action must run
#: them, compared word for word after continuations and spacing are collapsed. Exact, not a
#: list of flags each must carry: a flag check passes a second `-r`, a `<(...)`, an escaped
#: space or a trailing `& pip install`, and every review of the looser check found another
#: one. A deliberate change edits the action and this table in one diff, where it is read.
#: What it holds is how the toolchain is fetched and run; a command that fetches and runs
#: nothing (a `mkdir`, an `echo`) is the reviewer's to read, as release.yml's guards are.
LOCKED_COMMANDS = {
    "uv install": (
        re.compile(r"\bpip install\b"),
        (
            '"$RUNNER_TEMP/uv-venv/bin/python" -m pip install --quiet --disable-pip-version-check'
            ' --require-hashes --no-deps --only-binary :all: -r "$RUNNER_TEMP/uv-requirement.txt"'
        ),
    ),
    "toolchain export": (
        re.compile(r"\buv export\b"),
        (
            "uv export --no-cache --quiet --locked --only-group build --no-emit-project"
            ' --format requirements.txt --output-file "$RUNNER_TEMP/build-constraints.txt"'
        ),
    ),
    "build": (
        BUILDER,
        (
            'uv build --build-constraints "$RUNNER_TEMP/build-constraints.txt" --require-hashes'
            " --managed-python --no-cache"
        ),
    ),
}

#: The bootstrap that writes uv's hashed requirement for the install above to read.
LOCKED_BOOTSTRAP = 'python3 scripts/locked_requirement.py uv > "$RUNNER_TEMP/uv-requirement.txt"'

#: Every other command in the build action that runs uv, exactly: the check that the uv on
#: the PATH is the locked one.
UV_HELPERS = ('running="$(uv --version)"',)

#: uv or pip where the shell runs it: a command's first word (a path to it included), after
#: a separator or `$(`, or after `-m`. Not as an argument (`locked_requirement.py uv`) or in
#: a message (`echo "...uv..."`), which run nothing.
RUNS_UV_OR_PIP = re.compile(
    r"(?:^|[;&|(]\s*|-m\s+)(?:[^\s;&|()]*/)?(?:uv|pip3?)(?=[\"'\s)]|$)", re.MULTILINE
)

#: Setting what uv or pip read from their environment (an index, a constraint, hash
#: checking), or appending to the job's environment, changes what an exact command does.
TOOLCHAIN_ENVIRONMENT = re.compile(r"\b(?:UV|PIP)_[A-Z0-9_]+\s*[:=]|\bGITHUB_ENV\b")


def steps_using(text: str, action: str) -> list[tuple[int, list[str]]]:
    """Every step whose ``uses:`` names ``action`` (``owner/repo``), with the step's lines.

    Any ref, and case-insensitive, because Actions resolves ``Astral-SH/Setup-UV`` to the
    same repository: a rule that one capital letter steps around is no rule.
    """
    lines = text.splitlines()
    return [
        (ref.index + 1, _step_containing(lines, ref.index))
        for ref in uses_references(lines)
        if ref.action.split("@", 1)[0].lower() == action.lower()
    ]


def run_scripts(text: str) -> list[str]:
    """Each ``run:`` step's script in a workflow or action, line by line, raw.

    Comments, quotes and separators are left in, because nothing here parses shell. A
    check that something is absent reads the whole script, comments included, which errs
    toward finding an install or a build that is not there and never misses one that is.
    A check that a command is right takes it whole with ``command_at`` and compares it to
    the one it must be. A folded block (``>``) is NOT folded: its lines are kept, which is
    enough to find a match in, and the build action may not use one
    (``check_the_build_action_builds_from_the_lock``), so no exact command is ever read
    through YAML's folding rules. The scalar is bounded the way
    ``block_scalar_line_numbers`` bounds it, so a ``run:`` inside another script's text is
    not a step.
    """
    lines = text.splitlines()
    inside_a_script = block_scalar_line_numbers(lines)
    scripts = []
    for index, line in enumerate(lines):
        match = KEY_LINE.match(line)
        if index in inside_a_script or not match or match.group("key") != "run":
            continue
        value = _strip_comment(match.group("value") or "").strip()
        if not BLOCK_SCALAR.match(value):
            scripts.append(value)
            continue
        body = []
        for following in range(index + 1, len(lines)):
            if following not in inside_a_script:
                break
            # Leading indentation only: a line ending in `\ ` is an escaped space, not a
            # continuation, and `command_at` must not read it as one.
            body.append(lines[following].lstrip())
        while body and not body[-1].strip():
            body.pop()
        scripts.append("\n".join(body))
    return scripts


def command_at(script: str, position: int) -> str:
    """The whole command on the line holding ``position``, backslash continuations joined
    in both directions, exactly as written."""
    start = script.rfind("\n", 0, position) + 1
    while start > 1 and script[start - 2] == "\\":
        start = script.rfind("\n", 0, start - 1) + 1
    end = position
    while (newline := script.find("\n", end)) != -1 and script[newline - 1] == "\\":
        end = newline + 1
    return script[start:] if newline == -1 else script[start:newline]


def commands_matching(scripts: list[str], pattern: re.Pattern[str]) -> list[str]:
    """Every command any match of ``pattern`` falls in, once per match."""
    return [
        command_at(script, match.start())
        for script in scripts
        for match in pattern.finditer(script)
    ]


def words(command: str) -> str:
    """``command`` with its continuations and runs of whitespace collapsed to one space,
    padded, so `` --flag `` finds a flag as a whole word and never inside a longer one."""
    return " " + " ".join(command.replace("\\\n", " ").split()) + " "


def locked_command_problems(what: str, scripts: list[str]) -> list[str]:
    """Why the scripts do not run exactly one ``what``, exactly as ``LOCKED_COMMANDS`` says."""
    pattern, expected = LOCKED_COMMANDS[what]
    found = commands_matching(scripts, pattern)
    if len(found) != 1:
        return [f"expected exactly one {what}, found {found}"]
    if words(found[0]) != words(expected):
        return [
            f"the {what} is not the locked one.\n    runs:   {found[0]!r}\n    locked: {expected!r}"
        ]
    return []


def _requirement_name(requirement: str) -> str:
    match = REQUIREMENT_NAME.match(requirement)
    assert match, f"cannot read a package name from {requirement!r}"
    # PEP 503's normalization, the one uv.lock records names in.
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def locked_uv_version(lock_text: str) -> str:
    """The one exact uv the lock holds: what setup-uv and the build both install."""
    lock = _toml(lock_text)
    try:
        version = str(locked_package(lock, "uv")["version"])
    except ValueError as error:
        raise AssertionError(
            f"{UV_VERSION_FILE} must hold exactly one `uv`, from PyPI, with a sha256 for "
            f"every file: the build installs it by those hashes, and setup-uv reads its "
            f"version (#346). {error}"
        ) from error
    assert EXACT_UV_VERSION.match(version), (
        f"{UV_VERSION_FILE} holds uv {version!r}: setup-uv installs only an X.Y.Z exactly."
    )
    return version


def check_setup_uv_reads_the_lock(name: str, text: str) -> None:
    """Assert every setup-uv step in a workflow installs the uv ``uv.lock`` holds."""
    wrong = []
    for line, step in steps_using(text, SETUP_UV):
        files = [value.strip("\"'") for value in step_values(step, "version-file")]
        if files != [UV_VERSION_FILE]:
            wrong.append(f"{name}:{line}: version-file is {files or 'unset'}")
        if step_values(step, "version"):
            wrong.append(f"{name}:{line}: a `version:` input overrides the lock")
    assert not wrong, (
        f"every setup-uv step must read `version-file: {UV_VERSION_FILE}` and set no "
        f"`version:`, so CI installs the uv the release build does, and Dependabot's bump "
        f"of the lock moves both (#346). Without the file, setup-uv installs the latest:\n"
        + "\n".join(wrong)
    )


def check_build_requirements_are_locked(pyproject_text: str, lock_text: str) -> None:
    """Assert the ``build`` group holds uv and every ``[build-system].requires`` entry,
    each with the same specifier, and the lock holds each with its hashes."""
    pyproject = _toml(pyproject_text)
    lock = _toml(lock_text)
    requires = pyproject.get("build-system", {}).get("requires")
    assert requires, "[build-system].requires is missing or empty: the backend comes from nowhere"
    group = pyproject.get("dependency-groups", {}).get("build")
    assert group, (
        "pyproject.toml has no `build` dependency group: nothing puts the build toolchain "
        "in uv.lock, so it resolves fresh on every tag (#346)"
    )
    declared = {_requirement_name(entry): entry for entry in group}
    wrong = []
    if "uv" not in declared:
        wrong.append("uv is not in the `build` group, so the lock holds no uv to install")
    for entry in requires:
        name = _requirement_name(entry)
        if name not in declared:
            wrong.append(f"`{entry}` is a build requirement the `build` group does not declare")
            continue
        if declared[name].replace(" ", "") != entry.replace(" ", ""):
            wrong.append(
                f"the `build` group's `{declared[name]}` and [build-system]'s `{entry}` are "
                f"two statements of one requirement, and only one of them is locked"
            )
        try:
            locked_package(lock, name)
        except ValueError as error:
            wrong.append(str(error))
    assert not wrong, (
        "every link of the build must be in uv.lock with its hashes, or `uv build` resolves "
        "it fresh on the day of the tag (#346):\n" + "\n".join(wrong)
    )


def check_the_build_action_builds_from_the_lock(text: str) -> None:
    """Assert the one build runs exactly the locked commands, and nothing it cannot read."""
    lines = text.splitlines()
    inside_a_script = block_scalar_line_numbers(lines)
    scripts = run_scripts(text)
    wrong = []
    not_literal = [
        line.strip()
        for index, line in enumerate(lines)
        if index not in inside_a_script
        and (match := KEY_LINE.match(line))
        and match.group("key") == "run"
        and not _strip_comment(match.group("value") or "").strip().startswith("|")
    ]
    if not_literal:
        wrong.append(
            f"every run here must be a literal (`|`) block: YAML folds any other form, a "
            f"plain multi-line value included, and no command is read through that folding "
            f"here: {not_literal}"
        )
    bootstrap = commands_matching(scripts, re.compile(r"locked_requirement\.py"))
    if [words(command) for command in bootstrap] != [words(LOCKED_BOOTSTRAP)]:
        wrong.append(f"uv's requirement is not written by one `{LOCKED_BOOTSTRAP}`: {bootstrap}")
    for what in LOCKED_COMMANDS:
        wrong += locked_command_problems(what, scripts)
    allowed = {words(c) for c in (*UV_HELPERS, *(e for _, e in LOCKED_COMMANDS.values()))}
    others = [
        command
        for command in commands_matching(scripts, RUNS_UV_OR_PIP)
        if not command.startswith("#") and words(command) not in allowed
    ]
    if others:
        wrong.append(
            f"a command runs uv or pip and is none of the locked ones (a global flag before "
            f"the subcommand still runs it): {others}"
        )
    if TOOLCHAIN_ENVIRONMENT.search(text):
        wrong.append(
            "it sets a UV_ or PIP_ variable, or writes $GITHUB_ENV: either changes what an "
            "exact command fetches or checks"
        )
    third_party = [r.action for r in uses_references(lines)]
    if third_party:
        wrong.append(
            f"it uses {third_party}: Dependabot never reads .github/actions/, so a pin here "
            f"would be left behind by every bump of the same action in the workflows"
        )
    assert not wrong, (
        f"{BUILD_ACTION.name} must build only with what uv.lock holds (#346):\n" + "\n".join(wrong)
    )


def check_the_job_builds_with_the_action(name: str, text: str, job: str) -> None:
    """Assert ``job`` runs the build action and installs no uv of its own."""
    body = job_bodies(text).get(job)
    assert body is not None, f"{name} has no `{job}` job"
    actions = [r.action.split("@", 1)[0].lower() for r in uses_references(body)]
    assert BUILD_ACTION_REF in actions, (
        f"{name}'s `{job}` job does not run {BUILD_ACTION_REF}, the one build the lock holds "
        f"(#346). Its uses: {actions}"
    )
    assert SETUP_UV not in actions, (
        f"{name}'s `{job}` job runs setup-uv: the build installs the locked uv by its hashes, "
        f"and setup-uv's checksum is fetched at run time for a uv newer than its table (#346)"
    )
    # Whether or not the workflow holds power: the build runs the backend's code in the
    # checkout, and nothing in a build job pushes.
    checkouts = steps_using("\n".join(body), CHECKOUT)
    assert checkouts, f"{name}'s `{job}` job checks nothing out, so it has nothing to build"
    keeping = [line for line, step in checkouts if not _drops_its_token(step)]
    assert not keeping, (
        f"{name}'s `{job}` job must check out with `persist-credentials: false`: the build "
        f"runs the build backend's code in that tree, and nothing there pushes (#341, #346)"
    )


def check_nothing_else_builds(name: str, text: str) -> None:
    """Assert no workflow builds a distribution outside the build action."""
    builds = commands_matching(run_scripts(text), BUILDER)
    assert not builds, (
        f"{name} builds outside {BUILD_ACTION_REF}, where the lock's constraints do not "
        f"reach (#346). Comments in a script count, so a build is never hidden in one: {builds}"
    )


def check_the_publish_path_installs_only_the_locked_uv(name: str, text: str) -> None:
    """Assert the publish path's one install, if any, is the hash-checked uv bootstrap, and
    nothing on it sets what uv or pip read from their environment."""
    assert not TOOLCHAIN_ENVIRONMENT.search(text), (
        f"{name} sets a UV_ or PIP_ variable, or writes $GITHUB_ENV, on the publish path: "
        f"either changes what the build action's exact commands fetch or check (#346)"
    )
    installs = commands_matching(run_scripts(text), INSTALLER)
    locked = words(LOCKED_COMMANDS["uv install"][1])
    loose = installs if len(installs) > 1 or any(words(c) != locked for c in installs) else []
    assert not loose, (
        f"{name} installs something the lock does not hold by its hashes (#346). The one "
        f"install allowed is the uv bootstrap's, exactly as LOCKED_COMMANDS has it; comments "
        f"in a script count:\n" + "\n".join(loose)
    )


def _drops_its_token(step: list[str]) -> bool:
    """Whether a checkout step says ``persist-credentials: false``. Quoted or not, and in
    any case: checkout reads the input as a string, so every spelling of false is the same
    false and is not worth a red CI."""
    values = step_values(step, "persist-credentials")
    return [value.strip("\"'").lower() for value in values] == ["false"]


def check_checkouts_leave_no_token(name: str, text: str) -> None:
    """Assert a workflow holding power checks out without persisting its token."""
    if not privilege_reasons(text):
        return
    persisting = []
    for line, step in steps_using(text, CHECKOUT):
        if not _drops_its_token(step):
            shown = step_values(step, "persist-credentials") or "unset (true by default)"
            persisting.append(f"{name}:{line}: persist-credentials is {shown}")
    assert not persisting, (
        f"{name} holds power, so every checkout in it must say `persist-credentials: false`. "
        f"Otherwise actions/checkout leaves the job's GITHUB_TOKEN in .git/config for every "
        f"later step to read, including whatever code the build runs (#341):\n"
        + "\n".join(persisting)
    )


def test_the_lock_holds_one_exact_uv():
    locked_uv_version(LOCK.read_text(encoding="utf-8"))


def test_the_setup_uv_scanner_finds_the_steps():
    """CI runs uv in four jobs, so a scan that finds no setup-uv step there has gone
    blind, and the check below passes on air."""
    assert steps_using(CI_WORKFLOW.read_text(encoding="utf-8"), SETUP_UV)


@pytest.mark.parametrize("workflow", OWNED_WORKFLOWS, ids=lambda path: path.name)
def test_every_setup_uv_step_reads_the_lock(workflow):
    check_setup_uv_reads_the_lock(workflow.name, workflow.read_text(encoding="utf-8"))


def test_the_build_toolchain_is_locked():
    check_build_requirements_are_locked(
        PYPROJECT.read_text(encoding="utf-8"), LOCK.read_text(encoding="utf-8")
    )


def test_the_build_action_builds_from_the_lock():
    check_the_build_action_builds_from_the_lock(BUILD_ACTION.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("workflow", "job"), [(RELEASE_WORKFLOW, "build"), (CI_WORKFLOW, "build")])
def test_the_release_and_ci_build_with_the_action(workflow, job):
    """CI's job is also one the gate needs: test_the_gate_depends_on_every_job holds that."""
    check_the_job_builds_with_the_action(workflow.name, workflow.read_text(encoding="utf-8"), job)


@pytest.mark.parametrize("workflow", OWNED_WORKFLOWS, ids=lambda path: path.name)
def test_nothing_else_builds(workflow):
    check_nothing_else_builds(workflow.name, workflow.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", [RELEASE_WORKFLOW, BUILD_ACTION], ids=lambda path: path.name)
def test_the_publish_path_installs_only_the_locked_uv(path):
    check_the_publish_path_installs_only_the_locked_uv(path.name, path.read_text(encoding="utf-8"))


def test_the_run_reader_reads_the_build_actions_scripts():
    """All three ``run:`` steps: a reader that lost one would let every check above pass on
    less than the build runs."""
    scripts = run_scripts(BUILD_ACTION.read_text(encoding="utf-8"))
    assert len(scripts) == 3
    assert scripts[-1].startswith("uv build --build-constraints")


@pytest.mark.parametrize("name", ["release.yml", "dependency-security-scan.yml"])
def test_the_checkout_scanner_finds_the_steps(name):
    assert steps_using((WORKFLOWS / name).read_text(encoding="utf-8"), CHECKOUT), name


@pytest.mark.parametrize("workflow", OWNED_WORKFLOWS, ids=lambda path: path.name)
def test_a_workflow_that_holds_power_leaves_no_token_in_its_checkouts(workflow):
    check_checkouts_leave_no_token(workflow.name, workflow.read_text(encoding="utf-8"))


def _locked(name: str, version: str = "1.0.0", **overrides: object) -> str:
    """A ``uv.lock`` package entry in miniature, fabricated: one wheel, one sdist."""
    entry = {
        "source": '{ registry = "https://pypi.org/simple" }',
        "sdist": '{ url = "https://example.com/a.tar.gz", hash = "sha256:' + "a" * 64 + '" }',
        "wheels": '[{ url = "https://example.com/a.whl", hash = "sha256:' + "b" * 64 + '" }]',
    }
    entry.update({key: str(value) for key, value in overrides.items()})
    body = "".join(f"{key} = {value}\n" for key, value in entry.items())
    return f'[[package]]\nname = "{name}"\nversion = "{version}"\n{body}\n'


class TestTheToolchainScannersThemselves:
    """Where the toolchain readers fire and where they do not, on fabricated inputs."""

    @staticmethod
    def workflow(*steps: str, power: bool = False) -> str:
        grant = "    permissions:\n      id-token: write\n" if power else ""
        return (
            "permissions:\n  contents: read\njobs:\n  a:\n"
            + grant
            + "    steps:\n"
            + "".join(steps)
        )

    UV = "      - uses: astral-sh/setup-uv@v10.3.3\n"
    CO = "      - uses: actions/checkout@v7\n"
    PYPROJECT = (
        '[dependency-groups]\nbuild = ["uv>=0.13", "hatchling>=1.27"]\n'
        '[build-system]\nrequires = ["hatchling>=1.27"]\n'
    )
    LOCK = _locked("hatchling", "1.33.0") + _locked("uv", "0.13.6")

    def test_reads_one_exact_uv(self):
        assert locked_uv_version(self.LOCK) == "0.13.6"

    @pytest.mark.parametrize(
        ("lock", "reason"),
        [
            ("", "0 `uv` packages"),
            (_locked("uv") + _locked("uv", "2.0.0"), "2 `uv` packages"),
            (_locked("uv", source='{ git = "https://example.com/uv" }'), "not from PyPI"),
            (_locked("uv", sdist='{ url = "u", hash = "md5:00" }'), "no sha256"),
            (_locked("uv", "0.13"), "X.Y.Z"),
            (_locked("uv", "0.14.0rc1"), "X.Y.Z"),
        ],
    )
    def test_refuses_a_lock_that_cannot_pin_uv(self, lock, reason):
        with pytest.raises(AssertionError, match=re.escape(reason)):
            locked_uv_version(lock)

    @pytest.mark.parametrize(
        "inputs",
        [
            "          version-file: uv.lock\n",
            "          version-file: 'uv.lock'\n",
            "          python-version: '3.13'\n          version-file: uv.lock  # #346\n",
        ],
    )
    def test_accepts_a_setup_uv_reading_the_lock(self, inputs):
        text = self.workflow(self.UV + "        with:\n" + inputs)
        check_setup_uv_reads_the_lock("w.yml", text)

    @pytest.mark.parametrize(
        "step",
        [
            UV,
            UV + "        with:\n          python-version: '3.13'\n",
            UV + "        with:\n          version-file: pyproject.toml\n",
            UV + "        with:\n          version-file: .tool-versions\n",
            UV + "        with:\n          version-file: uv.lock\n          version: latest\n",
            "      - uses: Astral-SH/Setup-UV@v10\n",
            "      - name: uv\n        uses: astral-sh/setup-uv@" + "a" * 40 + " # v10.3.3\n",
        ],
    )
    def test_refuses_a_setup_uv_that_picks_its_own_uv(self, step):
        with pytest.raises(AssertionError, match="setup-uv"):
            check_setup_uv_reads_the_lock("w.yml", self.workflow(step))

    @pytest.mark.parametrize(
        "inputs",
        [
            "        with: {version-file: uv.lock}\n",
            "        with:\n          version-file:\n            uv.lock\n",
            "        with:\n          python-version-file: uv.lock\n",
        ],
    )
    def test_a_version_file_it_cannot_read_fails_closed(self, inputs):
        """No blind-spot scan is needed here: the check demands a positive reading of
        the file, so any shape it cannot read is a step that fails it, never one that
        slips past it."""
        with pytest.raises(AssertionError, match="setup-uv"):
            check_setup_uv_reads_the_lock("w.yml", self.workflow(self.UV + inputs))

    def test_a_neighbours_version_file_does_not_vouch_for_a_step(self):
        text = self.workflow(
            self.UV,
            "      - uses: other/action@v1\n        with:\n          version-file: uv.lock\n",
        )
        with pytest.raises(AssertionError, match="version-file is unset"):
            check_setup_uv_reads_the_lock("w.yml", text)

    def test_a_setup_uv_named_only_in_a_script_is_not_a_step(self):
        text = self.workflow("      - run: |\n          echo 'uses: astral-sh/setup-uv@v1'\n")
        assert steps_using(text, SETUP_UV) == []

    @pytest.mark.parametrize(
        "pyproject",
        [
            PYPROJECT,
            PYPROJECT.replace('"hatchling>=1.27"', '"hatchling >= 1.27"'),
            PYPROJECT.replace('"hatchling>=1.27"', '"Hatchling>=1.27"'),
        ],
    )
    def test_accepts_a_locked_build_toolchain(self, pyproject):
        check_build_requirements_are_locked(pyproject, self.LOCK)

    @pytest.mark.parametrize(
        ("pyproject", "reason"),
        [
            ('[build-system]\nrequires = ["hatchling>=1.27"]\n', "no `build` dependency group"),
            ('[dependency-groups]\nbuild = ["uv"]\n', r"\[build-system\]"),
            ("[dependency-groups]\nbuild = []\n[build-system]\nrequires = []\n", "build-system"),
            (
                (
                    '[dependency-groups]\nbuild = ["hatchling>=1.27"]\n'
                    '[build-system]\nrequires = ["hatchling>=1.27"]\n'
                ),
                "uv is not in the `build` group",
            ),
            (
                '[dependency-groups]\nbuild = ["uv"]\n[build-system]\nrequires = ["hatchling"]\n',
                "does not declare",
            ),
            (
                (
                    '[dependency-groups]\nbuild = ["uv", "hatchling>=1.30"]\n'
                    '[build-system]\nrequires = ["hatchling>=1.27"]\n'
                ),
                "two statements of one requirement",
            ),
            (
                (
                    '[dependency-groups]\nbuild = ["uv", "hatchling>=1.27", "hatch-vcs"]\n'
                    '[build-system]\nrequires = ["hatchling>=1.27", "hatch-vcs"]\n'
                ),
                "0 `hatch-vcs` packages",
            ),
        ],
    )
    def test_refuses_a_build_toolchain_the_lock_does_not_hold(self, pyproject, reason):
        with pytest.raises(AssertionError, match=reason):
            check_build_requirements_are_locked(pyproject, self.LOCK)

    @pytest.mark.parametrize(
        ("written", "normalized"),
        [
            ("hatchling>=1.27", "hatchling"),
            ("Hatch_Fancy__PyPI.Readme>=1", "hatch-fancy-pypi-readme"),
            ("zope.interface", "zope-interface"),
        ],
    )
    def test_names_are_normalized_as_uv_lock_records_them(self, written, normalized):
        assert _requirement_name(written) == normalized

    def test_refuses_a_locked_backend_with_no_hashes(self):
        lock = _locked("hatchling", sdist='{ url = "u" }', wheels="[]") + _locked("uv")
        with pytest.raises(AssertionError, match="no sha256"):
            check_build_requirements_are_locked(self.PYPROJECT, lock)

    @pytest.mark.parametrize("header", ["|", "|-", "|+", ">", ">-", ">2", "|2-", "| # note"])
    def test_the_run_reader_reads_every_block_scalar(self, header):
        """Line by line, a folded block included: it is not folded here."""
        text = f"    - run: {header}\n        a --x\n        # y\n          --z\n\n    - run: ls\n"
        assert run_scripts(text) == ["a --x\n# y\n--z", "ls"]

    def test_the_run_reader_skips_a_commented_out_run(self):
        assert run_scripts("      # - run: uv build\n      - run: ls\n") == ["ls"]

    def test_the_run_reader_skips_a_run_inside_a_script(self):
        text = "      - run: |\n          cat <<EOF\n          run: uv build\n          EOF\n"
        assert run_scripts(text) == ["cat <<EOF\nrun: uv build\nEOF"]

    SCRIPT = "set -e\nfirst --x \\\n  --y \\\n  --z\n# pip install q\nlast"

    @pytest.mark.parametrize("at", ["first", "--y", "--z"])
    def test_a_command_is_read_whole_across_its_continuations(self, at):
        whole = "first --x \\\n  --y \\\n  --z"
        assert command_at(self.SCRIPT, self.SCRIPT.index(at)) == whole

    def test_a_command_is_one_line_without_a_continuation(self):
        assert command_at(self.SCRIPT, self.SCRIPT.index("last")) == "last"
        assert command_at(self.SCRIPT, 0) == "set -e"

    def test_an_install_in_a_comment_is_found(self):
        """Absence is read with comments in: it errs toward finding one."""
        assert commands_matching([self.SCRIPT], INSTALLER) == ["# pip install q"]

    def test_a_flag_is_a_whole_word(self):
        assert " --no-cache " not in words("uv build --no-cache-dir")
        assert " --no-cache " in words("uv build \\\n  --no-cache")

    @pytest.mark.parametrize(
        "suffix",
        [
            " # note",
            "; pip install q",
            " & pip install q",
            " | tee log",
            " -r extra.txt",
            " --x <(echo y)",
            " \\ --x",
        ],
    )
    def test_anything_added_to_a_locked_command_is_refused(self, suffix):
        expected = LOCKED_COMMANDS["build"][1]
        (problem,) = locked_command_problems("build", [expected + suffix])
        assert "is not the locked one" in problem

    @pytest.mark.parametrize(
        "line",
        [
            "uv build",
            "uv --quiet build",
            '"$RUNNER_TEMP/uv-venv/bin/uv" build',
            'running="$(uv --version)"',
            '"$X/bin/python" -m pip install q',
            "true; pip3 install q",
            "true && uv export",
        ],
    )
    def test_reads_where_uv_or_pip_runs(self, line):
        assert RUNS_UV_OR_PIP.search(line), line

    @pytest.mark.parametrize(
        "line",
        [
            'python3 scripts/locked_requirement.py uv > "$F"',
            'echo "::error::The uv on the PATH says $running"',
            'if [ "uv==$(printf %s "$v")" != "$locked" ]; then',
            "uv-bin/uvicorn run",
            "--only-group build",
        ],
    )
    def test_an_argument_or_a_message_is_not_a_run(self, line):
        assert not RUNS_UV_OR_PIP.search(line), line

    def test_a_locked_command_may_wrap_its_lines(self):
        expected = LOCKED_COMMANDS["build"][1]
        wrapped = expected.replace(" --managed-python", " \\\n  --managed-python")
        assert locked_command_problems("build", [wrapped]) == []

    @pytest.mark.parametrize(
        "command",
        ["uv build", "python -m build", "pyproject-build .", "pip wheel .", "hatch build"],
    )
    def test_refuses_a_build_outside_the_action(self, command):
        text = self.workflow(f"      - run: {command}\n")
        with pytest.raises(AssertionError, match="builds outside"):
            check_nothing_else_builds("w.yml", text)

    def test_a_build_named_in_a_comment_is_not_a_build(self):
        check_nothing_else_builds("w.yml", self.workflow("      # - run: uv build\n"))

    @pytest.mark.parametrize(
        "command",
        [
            "pip install uv",
            "python -m pip install --require-hashes --no-deps -r r.txt",
            "uv tool install hatch",
            "uvx hatch build",
            "curl -LsSf https://example.com/install.sh | sh",
            "sudo apt-get install -y python3-build",
            "pipx run build",
            "uv python install 3.13",
            "uv run --with twine twine check dist/*",
            "uv pip sync requirements.txt",
            "pip download hatchling",
            "python -m ensurepip",
            "conda install hatchling",
            "gh release download v1 --repo o/r",
        ],
    )
    def test_refuses_an_install_the_lock_does_not_hold(self, command):
        text = self.workflow(f"      - run: {command}\n")
        with pytest.raises(AssertionError, match="installs something"):
            check_the_publish_path_installs_only_the_locked_uv("w.yml", text)

    @pytest.mark.parametrize(
        "command",
        [
            "uv export --locked --only-group build --output-file c.txt",
            "uv build --require-hashes",
            "uv --version",
            "python3 -m venv venv",
            "git ls-remote origin refs/heads/main",
        ],
    )
    def test_a_command_that_installs_nothing_is_left_alone(self, command):
        text = self.workflow(f"      - run: {command}\n")
        check_the_publish_path_installs_only_the_locked_uv("w.yml", text)

    def test_accepts_the_hash_checked_bootstrap(self):
        command = LOCKED_COMMANDS["uv install"][1]
        check_the_publish_path_installs_only_the_locked_uv(
            "w.yml", self.workflow(f"      - run: {command}\n")
        )

    @pytest.mark.parametrize(
        "step",
        [
            CO,
            CO + "        with:\n          persist-credentials: true\n",
            CO + "        with:\n          persist-credentials: \n",
            CO + "        with:\n          fetch-depth: 0\n",
            "      - uses: Actions/Checkout@v7\n",
        ],
    )
    def test_refuses_a_checkout_that_keeps_its_token_where_power_is_held(self, step):
        with pytest.raises(AssertionError, match="persist-credentials"):
            check_checkouts_leave_no_token("w.yml", self.workflow(step, power=True))

    @pytest.mark.parametrize(
        "inputs",
        [
            "        with: {persist-credentials: false}\n",
            "        with:\n          persist-credentials:\n            false\n",
        ],
    )
    def test_a_persist_credentials_it_cannot_read_fails_closed(self, inputs):
        text = self.workflow(self.CO + inputs, power=True)
        with pytest.raises(AssertionError, match="persist-credentials"):
            check_checkouts_leave_no_token("w.yml", text)

    def test_accepts_a_checkout_that_drops_its_token(self):
        step = self.CO + "        with:\n          persist-credentials: false  # #341\n"
        check_checkouts_leave_no_token("w.yml", self.workflow(step, power=True))

    def test_a_workflow_holding_nothing_is_left_alone(self):
        check_checkouts_leave_no_token("w.yml", self.workflow(self.CO))

    def test_a_neighbours_persist_credentials_does_not_vouch_for_a_checkout(self):
        text = self.workflow(
            self.CO,
            "      - uses: other/action@v1\n        with:\n          persist-credentials: false\n",
            power=True,
        )
        with pytest.raises(AssertionError, match="unset"):
            check_checkouts_leave_no_token("w.yml", text)


class TestEachToolchainEditIsCaught:
    """The edits that unpin the toolchain again, each made to the real files."""

    @pytest.fixture(scope="module")
    @classmethod
    def release(cls) -> str:
        return RELEASE_WORKFLOW.read_text(encoding="utf-8")

    @pytest.fixture(scope="module")
    @classmethod
    def ci(cls) -> str:
        return CI_WORKFLOW.read_text(encoding="utf-8")

    @pytest.fixture(scope="module")
    @classmethod
    def action(cls) -> str:
        return BUILD_ACTION.read_text(encoding="utf-8")

    @pytest.fixture(scope="module")
    @classmethod
    def pyproject(cls) -> str:
        return PYPROJECT.read_text(encoding="utf-8")

    @pytest.fixture(scope="module")
    @classmethod
    def lock(cls) -> str:
        return LOCK.read_text(encoding="utf-8")

    def test_dropping_a_ci_jobs_version_file_is_caught(self, ci):
        """The test leg, whose setup-uv also sets a Python: the one most easily re-typed."""
        broken = _mutate_in_job(ci, "test", "          version-file: uv.lock\n", "")
        with pytest.raises(AssertionError, match="version-file is unset"):
            check_setup_uv_reads_the_lock(CI_WORKFLOW.name, broken)

    def test_overriding_a_ci_jobs_uv_is_caught(self, ci):
        broken = _mutate_in_job(
            ci,
            "types",
            "          version-file: uv.lock\n",
            "          version-file: uv.lock\n          version: latest\n",
        )
        with pytest.raises(AssertionError, match="overrides the lock"):
            check_setup_uv_reads_the_lock(CI_WORKFLOW.name, broken)

    def test_building_the_release_with_setup_uv_again_is_caught(self, release):
        broken = _mutate_in_job(
            release,
            "build",
            f"      - uses: {BUILD_ACTION_REF}\n",
            "      - uses: astral-sh/setup-uv@v10.3.3\n      - run: uv build\n",
        )
        with pytest.raises(AssertionError, match="does not run"):
            check_the_job_builds_with_the_action(RELEASE_WORKFLOW.name, broken, "build")
        with pytest.raises(AssertionError, match="builds outside"):
            check_nothing_else_builds(RELEASE_WORKFLOW.name, broken)

    def test_adding_setup_uv_beside_the_action_is_caught(self, release):
        old = f"      - uses: {BUILD_ACTION_REF}\n"
        broken = _mutate_in_job(
            release, "build", old, "      - uses: astral-sh/setup-uv@v10.3.3\n" + old
        )
        with pytest.raises(AssertionError, match="runs setup-uv"):
            check_the_job_builds_with_the_action(RELEASE_WORKFLOW.name, broken, "build")

    def test_dropping_cis_build_job_is_caught(self, ci):
        broken = _mutate_in_job(ci, "build", f"      - uses: {BUILD_ACTION_REF}\n", "")
        with pytest.raises(AssertionError, match="does not run"):
            check_the_job_builds_with_the_action(CI_WORKFLOW.name, broken, "build")

    def test_ungating_cis_build_job_is_caught(self, ci):
        """test_the_gate_depends_on_every_job is what holds a CI build that blocks nothing:
        dropped from the gate's needs, the job is one it reads as a job and not a need."""
        broken = _mutate(ci, "drift, build, trailers", "drift, trailers")
        assert set(ci_job_names(broken)) - {GATE_JOB} - set(gate_needs(broken) or []) == {"build"}

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ('uv build --build-constraints "$RUNNER_TEMP/build-constraints.txt" ', "uv build "),
            ('constraints.txt" --require-hashes \\\n', 'constraints.txt" \\\n'),
            ("--managed-python --no-cache\n", "--no-cache\n"),
            ("--managed-python --no-cache\n", "--managed-python\n"),
            (
                '--build-constraints "$RUNNER_TEMP/build-constraints.txt"',
                "--build-constraints constraints/hand.txt",
            ),
        ],
        ids=["constraints", "require-hashes", "managed-python", "no-cache", "other-file"],
    )
    def test_changing_the_build_is_caught(self, action, old, new):
        broken = _mutate(action, old, new)
        with pytest.raises(AssertionError, match="the build is not the locked one"):
            check_the_build_action_builds_from_the_lock(broken)

    #: The uv install's flags, as the action writes them.
    PIP_FLAGS = "--require-hashes --no-deps --only-binary :all: -r"

    @pytest.mark.parametrize("flag", LOCKED_PIP)
    def test_dropping_a_hash_check_from_the_uv_install_is_caught(self, action, flag):
        broken = _mutate(action, self.PIP_FLAGS, self.PIP_FLAGS.replace(f"{flag} ", ""))
        with pytest.raises(AssertionError, match="uv install is not the locked one"):
            check_the_build_action_builds_from_the_lock(broken)
        with pytest.raises(AssertionError, match="installs something"):
            check_the_publish_path_installs_only_the_locked_uv(BUILD_ACTION.name, broken)

    def test_a_second_requirement_file_is_caught(self, action):
        old = '-r "$RUNNER_TEMP/uv-requirement.txt"\n'
        broken = _mutate(action, old, '-r "$RUNNER_TEMP/uv-requirement.txt" -r extra.txt\n')
        with pytest.raises(AssertionError, match="installs something"):
            check_the_publish_path_installs_only_the_locked_uv(BUILD_ACTION.name, broken)

    def test_a_plain_multi_line_run_is_caught(self, action):
        """YAML folds a plain value's continuation lines too: `&& pip install q` would run."""
        old = "      run: |\n        uv build"
        broken = _mutate(action, old, "      run: uv build")
        broken = _mutate(
            broken,
            "          --managed-python --no-cache\n",
            "          --managed-python --no-cache\n          && pip install q\n",
        )
        with pytest.raises(AssertionError, match="literal"):
            check_the_build_action_builds_from_the_lock(broken)

    @pytest.mark.parametrize(
        "second",
        [
            "uv --quiet build --no-build-isolation",
            'uv --no-cache export --frozen --output-file "$RUNNER_TEMP/build-constraints.txt"',
            '"$RUNNER_TEMP/uv-venv/bin/python" -m pip --isolated install uv',
        ],
    )
    def test_a_second_toolchain_command_behind_a_global_flag_is_caught(self, action, second):
        old = "          --managed-python --no-cache\n"
        broken = _mutate(action, old, old + f"        {second}\n")
        with pytest.raises(AssertionError, match="none of the locked ones"):
            check_the_build_action_builds_from_the_lock(broken)

    @pytest.mark.parametrize(
        "addition",
        [
            "      env:\n        UV_INDEX_URL: https://example.com/simple\n",
            "      env:\n        PIP_INDEX_URL: https://example.com/simple\n",
        ],
    )
    def test_a_toolchain_environment_on_the_build_step_is_caught(self, action, addition):
        old = "    - name: Build the wheel and sdist\n      shell: bash\n"
        broken = _mutate(action, old, old + addition)
        with pytest.raises(AssertionError, match="UV_ or PIP_"):
            check_the_build_action_builds_from_the_lock(broken)

    def test_writing_the_jobs_environment_is_caught(self, action):
        old = '        echo "$RUNNER_TEMP/uv-bin" >> "$GITHUB_PATH"\n'
        broken = _mutate(
            action, old, old + '        echo "UV_CONSTRAINT=/tmp/x" >> "$GITHUB_ENV"\n'
        )
        with pytest.raises(AssertionError, match="GITHUB_ENV"):
            check_the_build_action_builds_from_the_lock(broken)

    def test_a_toolchain_environment_in_the_release_is_caught(self, release):
        old = "    runs-on: ubuntu-latest\n    steps:\n"
        broken = _mutate_in_job(
            release,
            "build",
            old,
            "    runs-on: ubuntu-latest\n    env:\n      UV_NO_VERIFY_HASHES: '1'\n    steps:\n",
        )
        with pytest.raises(AssertionError, match="UV_ or PIP_"):
            check_the_publish_path_installs_only_the_locked_uv(RELEASE_WORKFLOW.name, broken)

    def test_an_escaped_space_is_not_a_continuation(self, action):
        old = 'constraints.txt" --require-hashes \\\n'
        broken = _mutate(action, old, 'constraints.txt" --require-hashes \\ \n')
        with pytest.raises(AssertionError, match="build"):
            check_the_build_action_builds_from_the_lock(broken)

    def test_a_folded_build_step_is_caught(self, action):
        broken = _mutate(
            action, "      run: |\n        uv build", "      run: >-\n        uv build"
        )
        with pytest.raises(AssertionError, match="literal"):
            check_the_build_action_builds_from_the_lock(broken)

    def test_a_second_install_beside_the_locked_one_is_caught(self, action):
        old = '          --require-hashes --no-deps --only-binary :all: -r "$RUNNER_TEMP/uv-requirement.txt"\n'
        broken = _mutate(action, old, old + "        pip install hatchling\n")
        with pytest.raises(AssertionError, match="installs something"):
            check_the_publish_path_installs_only_the_locked_uv(BUILD_ACTION.name, broken)

    def test_an_install_chained_after_the_locked_one_is_caught(self, action):
        old = '-r "$RUNNER_TEMP/uv-requirement.txt"\n'
        broken = _mutate(action, old, '-r "$RUNNER_TEMP/uv-requirement.txt" & pip install q\n')
        with pytest.raises(AssertionError, match="installs something"):
            check_the_publish_path_installs_only_the_locked_uv(BUILD_ACTION.name, broken)

    def test_an_install_from_another_file_is_caught(self, action):
        old = '-r "$RUNNER_TEMP/uv-requirement.txt"\n'
        broken = _mutate(action, old, '-r "$RUNNER_TEMP/other.txt"\n')
        with pytest.raises(AssertionError, match="uv install is not the locked one"):
            check_the_build_action_builds_from_the_lock(broken)

    def test_an_install_after_a_quoted_hash_in_the_release_is_caught(self, release):
        old = "      - uses: ./.github/actions/build-distributions\n"
        script = '      - run: |\n          echo "see #346"; pip install q\n'
        broken = _mutate(release, old, old + script)
        with pytest.raises(AssertionError, match="installs something"):
            check_the_publish_path_installs_only_the_locked_uv(RELEASE_WORKFLOW.name, broken)

    def test_a_flag_left_only_in_a_comment_is_caught(self, action):
        old = 'constraints.txt" --require-hashes \\\n'
        broken = _mutate(action, old, 'constraints.txt" \\\n          # --require-hashes\n')
        with pytest.raises(AssertionError, match="the build is not the locked one"):
            check_the_build_action_builds_from_the_lock(broken)

    def test_dropping_cis_build_checkout_guard_is_caught(self, ci):
        broken = _mutate_in_job(
            ci, "build", "        with:\n          persist-credentials: false\n", ""
        )
        with pytest.raises(AssertionError, match="persist-credentials: false"):
            check_the_job_builds_with_the_action(CI_WORKFLOW.name, broken, "build")

    def test_exporting_through_the_cache_is_caught(self, action):
        broken = _mutate(action, "uv export --no-cache ", "uv export ")
        with pytest.raises(AssertionError, match="toolchain export is not the locked one"):
            check_the_build_action_builds_from_the_lock(broken)

    def test_exporting_the_wrong_group_is_caught(self, action):
        broken = _mutate(action, "--only-group build", "--only-group dev")
        with pytest.raises(AssertionError, match="toolchain export is not the locked one"):
            check_the_build_action_builds_from_the_lock(broken)

    def test_a_third_party_action_in_the_build_is_caught(self, action):
        broken = action + "    - uses: astral-sh/setup-uv@v10.3.3\n"
        with pytest.raises(AssertionError, match="Dependabot never reads"):
            check_the_build_action_builds_from_the_lock(broken)

    @pytest.mark.parametrize("loose", ["hatchling", "hatchling>=1.30"])
    def test_moving_one_statement_of_the_backend_is_caught(self, pyproject, lock, loose):
        broken = _mutate(pyproject, 'requires = ["hatchling>=1.27"]', f'requires = ["{loose}"]')
        with pytest.raises(AssertionError, match="two statements"):
            check_build_requirements_are_locked(broken, lock)

    def test_a_second_build_requirement_the_group_lacks_is_caught(self, pyproject, lock):
        broken = _mutate(
            pyproject,
            'requires = ["hatchling>=1.27"]',
            'requires = ["hatchling>=1.27", "hatch-vcs"]',
        )
        with pytest.raises(AssertionError, match="does not declare"):
            check_build_requirements_are_locked(broken, lock)

    def test_dropping_uv_from_the_group_is_caught(self, pyproject, lock):
        broken = _mutate(pyproject, 'build = ["uv>=0.13.6", ', "build = [")
        with pytest.raises(AssertionError, match="uv is not in the `build` group"):
            check_build_requirements_are_locked(broken, lock)

    @pytest.mark.parametrize(("value", "reported"), [("true", "is ['true']"), ("", "unset")])
    def test_letting_the_release_build_keep_its_token_is_caught(self, release, value, reported):
        old = "          persist-credentials: false\n"
        new = f"          persist-credentials: {value}\n" if value else ""
        broken = _mutate_in_job(release, "build", old, new)
        with pytest.raises(AssertionError, match=re.escape(reported)):
            check_checkouts_leave_no_token(RELEASE_WORKFLOW.name, broken)

    def test_a_quoted_false_is_not_read_as_keeping_the_token(self, release):
        """Quoted, it is the string "false", which checkout also reads as off: the check
        must not redden CI over a spelling that does what it says."""
        old = "          persist-credentials: false\n"
        broken = _mutate_in_job(release, "build", old, "          persist-credentials: 'false'\n")
        check_checkouts_leave_no_token(RELEASE_WORKFLOW.name, broken)

    def test_letting_the_scan_keep_its_token_is_caught(self):
        scan = (WORKFLOWS / "dependency-security-scan.yml").read_text(encoding="utf-8")
        broken = _mutate(scan, "          persist-credentials: false\n", "")
        with pytest.raises(AssertionError, match="unset"):
            check_checkouts_leave_no_token("dependency-security-scan.yml", broken)


# --- The locked uv is one setup-uv carries a checksum for (live) ------------------------
#
# The release installs uv by the lock's own sha256s, so setup-uv is off the publish path.
# CI's setup-uv steps still install the uv the lock holds, and setup-uv checks the archive
# it downloads against the checksum table built into its own code only when that table
# holds the version: for a newer uv it trusts a checksum fetched at run time from the host
# that served the archive. #341 asked for a uv the table holds and nothing checked it; once
# the lock moved uv into Dependabot's hands, nothing would have held it at all (#346). The
# table lives in setup-uv's repository, so the check needs the network and runs in the
# drift-guard job, which runs on the 3.10 the repository pins: the lock is read here by a
# narrow pattern, and an offline test holds that pattern to tomllib's reading.

SETUP_UV_CHECKSUM_TABLE = "src/download/checksum/known-checksums.json"

#: ``ubuntu-latest``'s target, the platform half of the table's ``<target>-<version>`` keys.
RUNNER_TARGET = "x86_64-unknown-linux-gnu"

#: ``uv.lock``'s uv entry: the header, then its ``name`` and ``version`` lines.
LOCKED_UV_ENTRY = re.compile(r'^\[\[package\]\]\nname = "uv"\nversion = "([^"\n]*)"$', re.MULTILINE)


def locked_uv_version_by_pattern(lock_text: str) -> str:
    """The lock's one uv version, without ``tomllib`` (the drift-guard runs on 3.10)."""
    versions = LOCKED_UV_ENTRY.findall(lock_text)
    assert len(versions) == 1, (
        f"{UV_VERSION_FILE} has {len(versions)} entries this pattern reads as uv's, not one: "
        f"uv.lock's layout changed, so re-read it and fix LOCKED_UV_ENTRY"
    )
    return versions[0]


def setup_uv_ref(texts: dict[str, str]) -> str:
    """The one ref every setup-uv step names: one action, one table to check against."""
    refs = {
        f"{ref.action.split('@', 1)[1] if '@' in ref.action else ''}"
        for text in texts.values()
        for ref in uses_references(text.splitlines())
        if ref.action.split("@", 1)[0].lower() == SETUP_UV
    }
    assert len(refs) == 1 and "" not in refs, (
        f"every setup-uv step must name the same ref, so one checksum table is the one to "
        f"check the locked uv against; found {sorted(refs)}"
    )
    return refs.pop()


def check_the_table_holds_the_locked_uv(version: str, table: dict[str, str], ref: str) -> None:
    key = f"{RUNNER_TARGET}-{version}"
    assert key in table, (
        f"uv.lock holds uv {version}, and setup-uv@{ref}'s checksum table has no `{key}`, so "
        f"CI would install that uv checked only against a checksum fetched from the host "
        f"that served it (#346). Dependabot opens the uv bump and the setup-uv bump as separate "
        f"pull requests: leave the uv bump red until a setup-uv release whose table carries "
        f"uv {version} has merged, then update its branch and it re-runs green."
    )


def fetch_checksum_table(ref: str) -> dict[str, str]:
    """setup-uv's table at ``ref``, anonymously, retried as ``ls_remote_tags`` is."""
    url = f"https://raw.githubusercontent.com/{SETUP_UV}/{ref}/{SETUP_UV_CHECKSUM_TABLE}"
    failure = ""
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                table = json.load(response)
        except (OSError, ValueError) as error:
            failure = str(error)
        else:
            assert isinstance(table, dict) and table, f"{url} is not a checksum table"
            return table
        if attempt < 3:
            time.sleep(attempt * 5)
    raise AssertionError(
        f"fetching {url} failed three times, so whether setup-uv knows the locked uv's "
        f"checksum is unknown. Last failure: {failure}"
    )


@pytest.mark.live
def test_the_locked_uv_is_one_setup_uv_has_a_checksum_for():
    ref = setup_uv_ref({w.name: w.read_text(encoding="utf-8") for w in OWNED_WORKFLOWS})
    version = locked_uv_version_by_pattern(LOCK.read_text(encoding="utf-8"))
    check_the_table_holds_the_locked_uv(version, fetch_checksum_table(ref), ref)


class TestTheChecksumTableCheckItself:
    """The offline half of the live check, on fabricated tables."""

    def test_the_pattern_reads_the_lock_as_tomllib_does(self):
        text = LOCK.read_text(encoding="utf-8")
        assert locked_uv_version_by_pattern(text) == locked_uv_version(text)

    @pytest.mark.parametrize(
        "lock",
        [
            "",
            _locked("uv") + _locked("uv", "2.0.0"),
            _locked("uvicorn"),
            '[[package]]\nversion = "1.0.0"\nname = "uv"\n',
        ],
    )
    def test_a_lock_the_pattern_cannot_read_as_one_uv_fails(self, lock):
        with pytest.raises(AssertionError, match="LOCKED_UV_ENTRY"):
            locked_uv_version_by_pattern(lock)

    def test_reads_the_one_setup_uv_ref(self):
        texts = {
            "a.yml": "jobs:\n  a:\n    steps:\n      - uses: astral-sh/setup-uv@v10.3.3\n",
            "b.yml": "jobs:\n  b:\n    steps:\n      - uses: Astral-SH/Setup-UV@v10.3.3\n",
        }
        assert setup_uv_ref(texts) == "v10.3.3"

    @pytest.mark.parametrize(
        "second",
        ["      - uses: astral-sh/setup-uv@v10.4.0\n", "      - uses: astral-sh/setup-uv\n"],
    )
    def test_two_setup_uv_refs_or_none_fail(self, second):
        texts = {
            "a.yml": "jobs:\n  a:\n    steps:\n      - uses: astral-sh/setup-uv@v10.3.3\n",
            "b.yml": "jobs:\n  b:\n    steps:\n" + second,
        }
        with pytest.raises(AssertionError, match="same ref"):
            setup_uv_ref(texts)

    def test_a_table_holding_the_version_passes(self):
        table = {f"{RUNNER_TARGET}-0.13.6": "a" * 64}
        check_the_table_holds_the_locked_uv("0.13.6", table, "v10.3.3")

    @pytest.mark.parametrize(
        "table",
        [
            {f"{RUNNER_TARGET}-0.13.5": "a" * 64},
            {"aarch64-unknown-linux-gnu-0.13.6": "a" * 64},
            {f"{RUNNER_TARGET}-0.13.60": "a" * 64},
        ],
    )
    def test_a_table_without_the_version_fails(self, table):
        with pytest.raises(AssertionError, match="no `x86_64-unknown-linux-gnu-0.13.6`"):
            check_the_table_holds_the_locked_uv("0.13.6", table, "v10.3.3")


# --- The pin comment names the pinned commit (live) -------------------------------------


class ActionPin(NamedTuple):
    """One SHA-pinned ``uses:`` and the tag its version comment names."""

    where: str  # workflow:line
    repository: str  # owner/repo, where the tag lives
    sha: str
    tag: str


def action_pins(name: str, text: str) -> list[ActionPin]:
    """Every SHA pin in a workflow that carries a version comment, with that comment's tag.

    The tag is the comment's first word, exactly as written: Dependabot writes the
    action's own tag name there, so ``# 7.0.1`` against a repository that tags
    ``v7.0.1`` is a comment naming a tag that does not exist, and is reported as one.
    """
    pins = []
    for ref in uses_references(text.splitlines()):
        if not SHA_PINNED.match(ref.action) or not VERSION_COMMENT.match(ref.comment):
            continue
        path, sha = ref.action.split("@")
        tag = ref.comment.lstrip("#").split()[0]
        repository = "/".join(path.split("/")[:2])
        pins.append(ActionPin(f"{name}:{ref.index + 1}", repository, sha, tag))
    return pins


def resolved_tags(ls_remote_output: str) -> dict[str, str]:
    """``git ls-remote`` output → each tag's commit, peeled through an annotated tag.

    An annotated tag's ref names the tag *object*; the commit is on its ``^{}`` line, and
    that commit is what a pin names. A lightweight tag has no ``^{}`` line, and its ref is
    the commit itself. Read the object as the commit and every annotated pin looks wrong.
    """
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in ls_remote_output.splitlines():
        sha, _, ref = line.strip().partition("\t")
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/")
        if tag.endswith("^{}"):
            peeled[tag.removesuffix("^{}")] = sha
        else:
            direct[tag] = sha
    return {tag: peeled.get(tag, sha) for tag, sha in direct.items()}


def pin_comment_mismatches(
    pins: list[ActionPin], tags_by_repository: dict[str, dict[str, str]]
) -> list[str]:
    """Every pin whose comment's tag is not, today, the pinned commit."""
    mismatches = []
    for pin in pins:
        commit = tags_by_repository.get(pin.repository, {}).get(pin.tag)
        if commit is None:
            mismatches.append(f"{pin.where}: # {pin.tag} -- {pin.repository} has no such tag")
        elif commit != pin.sha:
            mismatches.append(
                f"{pin.where}: # {pin.tag} -- that tag is {commit}, not the pinned {pin.sha}"
            )
    return mismatches


def ls_remote_tags(repository: str, tags: set[str], cwd: Path) -> str:
    """One anonymous ``git ls-remote`` of these tags, both the ref and its peeled form.

    Anonymous on purpose, and wholly: the repositories are public, and git over HTTPS
    does not draw on the REST API's 60-an-hour allowance for unauthenticated callers. Run
    from ``cwd``, an empty directory, rather than the checkout, because ci.yml's checkouts
    persist the job's ``GITHUB_TOKEN`` into ``.git/config`` as an
    ``http.https://github.com/.extraheader``, which git would send to every repository on
    github.com. Global and system config are ignored too, so a credential helper on a
    contributor's machine is never asked. ``GIT_TERMINAL_PROMPT=0`` because GitHub answers
    a repository that does not exist with a credential prompt, which would hang the job
    instead of failing it.

    Three attempts, as ci.yml's actionlint pull makes, because this job gates every merge:
    a blip on GitHub's side, an error or a stall, must not redden a PR that touched no
    workflow. A comment that does not match its pin fails on the first answer; only a
    failure to get one is retried.
    """
    patterns = [f"refs/tags/{tag}{suffix}" for tag in sorted(tags) for suffix in ("", "^{}")]
    # The caller's environment minus everything that steers git to a credential or a
    # repository: GIT_ASKPASS is asked before GIT_TERMINAL_PROMPT is consulted (an IDE
    # terminal sets one), and GIT_DIR or GIT_CONFIG_* from a hook would bring a repo's own
    # config back. Proxy settings and PATH stay, or git could not reach GitHub at all.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key != "SSH_ASKPASS"
    }
    env |= {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        # And no repository above `cwd` is found either, wherever pytest's basetemp is.
        "GIT_CEILING_DIRECTORIES": str(cwd.resolve().parent),
    }
    failure = ""
    for attempt in (1, 2, 3):
        try:
            result = subprocess.run(
                ["git", "ls-remote", f"https://github.com/{repository}.git", *patterns],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            failure = "no answer within 60 s"
        else:
            if result.returncode == 0:
                return result.stdout
            failure = f"exit {result.returncode}: {result.stderr.strip()}"
        if attempt < 3:
            time.sleep(attempt * 5)
    raise AssertionError(
        f"`git ls-remote` of {repository} failed three times, so whether its pins' comments "
        f"are true is unknown. Last failure: {failure}"
    )


@pytest.mark.live
def test_every_pin_comment_names_the_pinned_commit(tmp_path):
    """A pin's ``# vX.Y.Z`` is what a reviewer of its next bump reads as the starting
    point, and #339's guard checked only that one is there. Read each tag off the action's
    own repository and hold the comment to the commit."""
    pins = [
        pin
        for workflow in OWNED_WORKFLOWS
        for pin in action_pins(workflow.name, workflow.read_text(encoding="utf-8"))
    ]
    assert pins, "no SHA pins with version comments found: the reader has gone blind"
    tags_by_repository = {
        repository: resolved_tags(
            ls_remote_tags(
                repository, {pin.tag for pin in pins if pin.repository == repository}, tmp_path
            )
        )
        for repository in sorted({pin.repository for pin in pins})
    }
    mismatches = pin_comment_mismatches(pins, tags_by_repository)
    assert not mismatches, (
        "a pin's version comment must name the tag whose commit it pins. Re-read the SHA "
        "off the action's repository (`git ls-remote https://github.com/OWNER/REPO "
        "'refs/tags/vX.Y.Z^{}'`, falling back to the bare tag for a lightweight one) and "
        "fix whichever side is wrong (#341). If the comment was right when it landed, the "
        "tag has moved upstream: what runs is still the pinned commit, but a moved release "
        "tag is how a compromised action reaches its users, so find out why before editing "
        "either side:\n" + "\n".join(mismatches)
    )


#: ``git ls-remote``'s answer in miniature: one lightweight tag, and one annotated (the tag
#: object, then its peeled commit). The SHAs are the real ones for those two tags, so the
#: fixture shows the very case that makes peeling necessary; nothing here reads them live.
LS_REMOTE = (
    "3d3c42e5aac5ba805825da76410c181273ba90b1\trefs/tags/v7.0.1\n"
    "aef6c9b6e301dc803e1bc9133e9ce67ae9b0906b\trefs/tags/v1.14.3\n"
    "7245906fb61fa33a13554f9d5fb856fe61758086\trefs/tags/v1.14.3^{}\n"
)


class TestThePinCommentCheckItself:
    """The offline half of the live check: the parse and the comparison."""

    def test_reads_the_pins_and_their_tags(self):
        text = (
            "jobs:\n  a:\n    steps:\n"
            f"      - uses: {PINNED} # v7.0.1\n"
            "      - uses: o/r/sub@" + "b" * 40 + "  # v2.0.0 (moved 2026-11-16)\n"
            "      - uses: actions/checkout@v7\n"
            f"      - uses: {PINNED}\n"
            "      - run: |\n          echo 'uses: o/r@" + "c" * 40 + " # v9'\n"
        )
        assert action_pins("w.yml", text) == [
            ActionPin("w.yml:4", "actions/checkout", PINNED.split("@")[1], "v7.0.1"),
            ActionPin("w.yml:5", "o/r", "b" * 40, "v2.0.0"),
        ]

    def test_an_annotated_tag_is_peeled_to_its_commit(self):
        tags = resolved_tags(LS_REMOTE)
        assert tags == {
            "v7.0.1": "3d3c42e5aac5ba805825da76410c181273ba90b1",
            "v1.14.3": "7245906fb61fa33a13554f9d5fb856fe61758086",
        }

    def test_a_true_comment_reports_nothing(self):
        pin = ActionPin("w.yml:1", "o/r", "7245906fb61fa33a13554f9d5fb856fe61758086", "v1.14.3")
        assert pin_comment_mismatches([pin], {"o/r": resolved_tags(LS_REMOTE)}) == []

    def test_a_comment_naming_the_tag_object_rather_than_the_commit_is_caught(self):
        pin = ActionPin("w.yml:1", "o/r", "aef6c9b6e301dc803e1bc9133e9ce67ae9b0906b", "v1.14.3")
        (mismatch,) = pin_comment_mismatches([pin], {"o/r": resolved_tags(LS_REMOTE)})
        assert "not the pinned" in mismatch

    def test_a_comment_naming_a_missing_tag_is_caught(self):
        pin = ActionPin("w.yml:1", "o/r", "3d3c42e5aac5ba805825da76410c181273ba90b1", "7.0.1")
        (mismatch,) = pin_comment_mismatches([pin], {"o/r": resolved_tags(LS_REMOTE)})
        assert "no such tag" in mismatch

    def test_a_repository_that_returned_nothing_is_caught(self):
        pin = ActionPin("w.yml:1", "o/r", "3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1")
        assert pin_comment_mismatches([pin], {}) != []


class TestEachPinCommentEditIsCaught:
    """Edits to the real release.yml, checked against a remote that agrees with it today."""

    @pytest.fixture(scope="module")
    @classmethod
    def release(cls) -> str:
        return RELEASE_WORKFLOW.read_text(encoding="utf-8")

    @pytest.fixture(scope="module")
    @classmethod
    def remote(cls, release) -> dict[str, dict[str, str]]:
        """What the actions' repositories would say if every comment were true."""
        tags: dict[str, dict[str, str]] = {}
        for pin in action_pins(RELEASE_WORKFLOW.name, release):
            tags.setdefault(pin.repository, {})[pin.tag] = pin.sha
        return tags

    def test_the_real_file_agrees_with_a_remote_built_from_it(self, release, remote):
        pins = action_pins(RELEASE_WORKFLOW.name, release)
        assert {pin.repository for pin in pins} >= {
            "actions/checkout",
            "actions/upload-artifact",
            "pypa/gh-action-pypi-publish",
        }
        assert pin_comment_mismatches(pins, remote) == []

    def test_a_comment_naming_another_version_is_caught(self, release, remote):
        pin = next(
            p for p in action_pins("r", release) if p.repository == "actions/upload-artifact"
        )
        broken = _mutate(release, f"{pin.sha} # {pin.tag}", f"{pin.sha} # v0.0.1")
        mismatches = pin_comment_mismatches(action_pins("r", broken), remote)
        assert any("# v0.0.1" in m and "no such tag" in m for m in mismatches), mismatches

    def test_a_sha_moved_without_its_comment_is_caught(self, release, remote):
        pin = next(
            p for p in action_pins("r", release) if p.repository == "actions/upload-artifact"
        )
        broken = _mutate(release, f"{pin.sha} # {pin.tag}", f"{'d' * 40} # {pin.tag}")
        mismatches = pin_comment_mismatches(action_pins("r", broken), remote)
        assert any("not the pinned" in m for m in mismatches), mismatches
