"""Four workflow invariants: bounded artifact retention, a gate that covers every job, a
gate that cannot fail open, and a release pipeline that publishes what it says it does.

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
    def ci(self):
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


class TestEachReleaseEditIsCaught:
    """Each fact this section pins, broken in the real ``release.yml`` and rejected by
    its own check — and beside each, the spellings that mean the same thing and must
    stay green. Both directions, because a check that reddens CI on a correct workflow
    is the one that gets deleted.
    """

    @pytest.fixture(scope="module")
    def release(self):
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
            "      - uses: pypa/gh-action-pypi-publish@release/v1\n",
            "      - uses: pypa/gh-action-pypi-publish@release/v1\n"
            "        with:\n          repository-url: https://example.invalid/legacy/\n",
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
            "      - uses: pypa/gh-action-pypi-publish@release/v1\n",
            "      - uses: pypa/gh-action-pypi-publish@release/v1\n"
            "        with:\n          repository_url: https://example.invalid/legacy/\n",
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
