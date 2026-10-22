"""The ``trailers`` job in ``ci.yml``, run for real against throwaway repositories.

CLAUDE.md -> Fleet Bot Identity: a commit authored by ``basecradle-python-ai[bot]`` carries
no ``Co-Authored-By`` trailer, and the job's *No Co-Authored-By on bot commits* step is what
enforces it. Most of the ways that step can be wrong live in its shell -- which commits it
walks, whose it reads, how much of each message it sees -- so this file *runs* the script
ci.yml ships, read out of the file, inside a git repository built per test under ``tmp_path``. A
guard whose only test greps for its own source line still passes when someone rewrites the
guard wrongly.

It exists because of #286: under ``pipefail``, ``printf "$message" | grep -q`` fails open.
``grep -q`` exits on its first match, ``printf`` dies of SIGPIPE writing the rest of a
message longer than the pipe buffer, and the pipeline's non-zero status reads as "no
trailer". A pasted log in a commit body was enough. Reported by the ruby captain
(basecradle-ruby#244) while porting this job; the ruby port's suite is the model here.

The script is found by text, not a YAML parse, for the reason ``test_workflows.py`` gives
(no dev dependency for a parser, #169): the step is located by its ``name:`` and its
``run: |`` block read by indentation, and a step this reader cannot find fails the suite
rather than skipping it. What this file does not read is the step's YAML around the script
-- its ``if:`` event gate, the checkout's ``fetch-depth`` -- and membership in the ``ci``
gate's ``needs`` is ``test_workflows.py``'s to pin, not this file's.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
CHARTER = Path(__file__).parent.parent / "CLAUDE.md"
STEP = "No Co-Authored-By on bot commits"

HUMAN = ("John Doe", "john@example.com")
TRAILER = "Co-Authored-By: Nova Digital <nova@example.com>"

#: Comfortably past Linux's 64 KiB pipe buffer, so a writer still has bytes left to send
#: when grep stops reading.
LONG_BODY = "A pasted log line.\n" * 20_000


class Step(NamedTuple):
    """The shipped step: its ``run:`` script and the ``BOT`` its ``env:`` sets."""

    script: str
    bot: str


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def read_step(text: str, name: str = STEP) -> Step:
    """The named step's ``run: |`` script, dedented, and its ``env:`` ``BOT`` value."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == f"- name: {name}"]
    assert len(starts) == 1, f"ci.yml has {len(starts)} steps named {name!r}, expected one"
    start = starts[0]
    key_indent = _indent(lines[start]) + 2

    # The step's body: everything until a line left of its keys. Blank lines and comments
    # carry no structure -- a comment may sit at any column -- so neither ends the step.
    body: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and _indent(line) < key_indent:
            break
        body.append(line)

    bots = [
        line.split(":", 1)[1].strip()
        for line in body
        if line.strip().startswith("BOT:") and _indent(line) > key_indent
    ]
    assert len(bots) == 1, f"step {name!r} sets BOT {len(bots)} times in its env, expected once"
    # Read bare: a quoted or commented value is a shape this reader would take differently
    # from Actions, so it is refused rather than passed through.
    assert re.fullmatch(r"[\w.-]+\[bot\]", bots[0]), f"cannot read BOT value {bots[0]!r}"

    runs = [i for i, line in enumerate(body) if line.strip() == "run: |"]
    assert len(runs) == 1, f"step {name!r} has {len(runs)} readable `run: |` blocks, expected one"
    assert _indent(body[runs[0]]) == key_indent, f"step {name!r}'s `run:` is not one of its keys"

    script: list[str] = []
    for line in body[runs[0] + 1 :]:
        if line.strip() and _indent(line) <= key_indent:
            break
        script.append(line)
    depth = min(_indent(line) for line in script if line.strip())
    return Step(script="\n".join(line[depth:] for line in script) + "\n", bot=bots[0])


class Run(NamedTuple):
    returncode: int
    output: str


class Repo:
    """A throwaway repository, isolated from the machine's git config, on a feature branch.

    Every commit is John Doe's unless ``commit`` names another author -- merge commits
    included -- so a signing hook or a default branch name set by whoever runs the suite
    cannot change what is being tested.
    """

    def __init__(self, path: Path, step: Step) -> None:
        self.path = path
        self.step = step
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(path),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            # The runner's locale: `${message,,}` and the pattern match are locale-aware.
            "LC_ALL": "C.UTF-8",
            "GIT_AUTHOR_NAME": HUMAN[0],
            "GIT_AUTHOR_EMAIL": HUMAN[1],
            "GIT_COMMITTER_NAME": HUMAN[0],
            "GIT_COMMITTER_EMAIL": HUMAN[1],
        }
        self.git("init", "-q", "-b", "main")
        (path / "CLAUDE.md").write_text(f"Commits are authored by `{step.bot}`.\n")
        self.commit("Seed the repository")
        self.base = self.rev_parse("HEAD")
        self.git("switch", "-q", "-c", "feature")

    @property
    def bot(self) -> tuple[str, str]:
        return (self.step.bot, f"290976240+{self.step.bot}@users.noreply.github.com")

    def git(self, *args: str, stdin: str = "", env: dict[str, str] | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env={**self.env, **(env or {})},
            input=stdin,
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode == 0, f"git {' '.join(args)} failed:\n{result.stderr}"
        return result.stdout

    def commit(self, message: str, author: tuple[str, str] = HUMAN) -> None:
        with (self.path / "change.txt").open("a") as change:
            change.write(f"{message}\n")
        self.git("add", "-A")
        # On stdin, not argv: one test's message is longer than an argument may be.
        self.git(
            "commit",
            "-q",
            "-F",
            "-",
            stdin=message,
            env={"GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1]},
        )

    def rev_parse(self, ref: str) -> str:
        return self.git("rev-parse", ref).strip()

    def run_step(self) -> Run:
        """The shipped script, with the env ci.yml gives it, under the shell GitHub gives a
        ``run:`` block with no ``shell:`` key (``bash -e``) -- so ``pipefail`` and ``-u`` come
        from the script's own ``set`` line, exactly as in CI."""
        env = {
            **self.env,
            "BOT": self.step.bot,
            "BASE_REF": "main",
            "BASE_SHA": self.base,
            "HEAD_SHA": self.rev_parse("HEAD"),
        }
        result = subprocess.run(
            ["bash", "-e", "-c", self.step.script],
            cwd=self.path,
            env=env,
            capture_output=True,
            check=False,
            text=True,
        )
        return Run(result.returncode, result.stdout + result.stderr)


def _bash_version() -> tuple[int, int]:
    result = subprocess.run(
        ["bash", "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'],
        capture_output=True,
        check=True,
        text=True,
    )
    major, minor = result.stdout.split()
    return int(major), int(minor)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    # The step runs on ubuntu's bash. Before 4.4, `"${bad[@]}"` on an empty array trips
    # `set -u` and `${message,,}` does not exist, so an older bash (macOS's /bin/bash is 3.2)
    # cannot run the script at all -- the machine's limit, not a finding about the step.
    if _bash_version() < (4, 4):
        pytest.skip("the step needs bash >= 4.4, as on the CI runner")
    return Repo(tmp_path, read_step(WORKFLOW.read_text()))


# --- what the step looks at ---


def test_a_bot_commit_without_a_trailer_passes(repo: Repo):
    repo.commit("Add a resource", author=repo.bot)

    run = repo.run_step()

    assert run.returncode == 0, run.output


def test_a_bot_commit_with_a_trailer_fails_and_names_the_commit(repo: Repo):
    repo.commit(f"Add a resource\n\n{TRAILER}", author=repo.bot)

    run = repo.run_step()

    assert run.returncode != 0, "a bot commit carrying a Co-Authored-By trailer must fail"
    assert "::error::" in run.output, "the finding must annotate the run"
    assert "Add a resource" in run.output, "the annotation must name the offending commit"


def test_a_trailer_that_is_not_in_the_last_paragraph_still_fails(repo: Repo):
    """git's ``%(trailers)`` parses only the final paragraph; the step scans every line."""
    repo.commit(f"Add a resource\n\n{TRAILER}\n\nA closing paragraph.", author=repo.bot)

    assert repo.run_step().returncode != 0, "a trailer with a paragraph after it slipped through"


def test_a_trailer_early_in_a_message_longer_than_the_pipe_buffer_still_fails(repo: Repo):
    """#286: the message must reach grep without a pipe that pipefail can turn into a pass."""
    repo.commit(f"Add a resource\n\n{TRAILER}\n\n{LONG_BODY}", author=repo.bot)

    run = repo.run_step()

    assert run.returncode != 0, (
        "a trailer near the top of a message longer than the pipe buffer slipped through -- "
        "`printf | grep -q` under pipefail reads SIGPIPE as no match"
    )
    assert "Add a resource" in run.output


def test_a_long_message_without_a_trailer_passes(repo: Repo):
    """The control for the test above: length alone is not what fails the step."""
    repo.commit(f"Add a resource\n\n{LONG_BODY}", author=repo.bot)

    run = repo.run_step()

    assert run.returncode == 0, run.output


def test_a_trailer_on_the_first_line_still_fails(repo: Repo):
    """Line-anchored means every line, the subject included."""
    repo.commit(TRAILER, author=repo.bot)

    assert repo.run_step().returncode != 0, "a trailer as the subject line slipped through"


def test_the_match_ignores_case(repo: Repo):
    repo.commit(f"Add a resource\n\n{TRAILER.lower()}", author=repo.bot)

    assert repo.run_step().returncode != 0, "`co-authored-by:` is the same trailer to GitHub"


def test_every_offending_commit_is_reported_not_just_the_first(repo: Repo):
    repo.commit(f"First change\n\n{TRAILER}", author=repo.bot)
    repo.commit(f"Second change\n\n{TRAILER}", author=repo.bot)

    run = repo.run_step()

    assert run.returncode != 0, run.output
    assert "First change" in run.output
    assert "Second change" in run.output


# --- what it must leave alone ---


def test_a_mid_line_mention_is_not_a_trailer(repo: Repo):
    """The scan is line-anchored: prose about the rule is not a violation of it."""
    repo.commit("Refuse the Co-Authored-By: trailer on bot commits", author=repo.bot)

    run = repo.run_step()

    assert run.returncode == 0, run.output


def test_a_human_commit_with_a_trailer_passes(repo: Repo):
    """The rule binds the bot's own commits; a human's co-author line is their call."""
    repo.commit(f"Pair on the README\n\n{TRAILER}")

    run = repo.run_step()

    assert run.returncode == 0, run.output


def test_commits_already_on_the_base_branch_are_not_examined(repo: Repo):
    """GitHub's squash-merge writes a self-co-author trailer onto main's commits, which can
    never be rewritten, so a branch that merged main in must not re-litigate them."""
    repo.git("switch", "-q", "main")
    repo.commit(f"Squash-merged earlier\n\n{TRAILER}", author=repo.bot)
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    repo.git("switch", "-q", "feature")
    repo.git("merge", "-q", "--no-edit", "main")
    repo.commit("Add a resource", author=repo.bot)

    run = repo.run_step()

    assert run.returncode == 0, (
        "a commit reachable from the live base branch was examined -- the walk must exclude "
        f"refs/remotes/origin/<base>, not only the event's base sha\n{run.output}"
    )


# --- its own failure modes ---


def test_an_identity_the_charter_no_longer_states_fails_instead_of_passing_everything(
    repo: Repo,
):
    """If CLAUDE.md and the step's BOT drift apart, the author filter matches nothing and
    every commit passes -- so the drift itself must fail."""
    (repo.path / "CLAUDE.md").write_text("Commits are authored by someone else.\n")
    repo.commit("Add a resource", author=repo.bot)

    run = repo.run_step()

    assert run.returncode != 0
    assert "::error::" in run.output


def test_the_identity_the_step_checks_is_the_one_this_charter_states():
    bot = read_step(WORKFLOW.read_text()).bot

    assert bot in CHARTER.read_text(), (
        f"ci.yml's trailers job checks commits by {bot!r}, which CLAUDE.md -> Fleet Bot "
        "Identity no longer names"
    )


# --- the reader itself ---


class TestTheStepReader:
    WORKFLOW_TEXT = (
        "jobs:\n"
        "  trailers:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v7\n"
        f"      - name: {STEP}\n"
        "        env:\n"
        "          BOT: someone[bot]\n"
        "        run: |\n"
        "          set -euo pipefail\n"
        "\n"
        "          if true; then\n"
        "            echo indented\n"
        "          fi\n"
        "      - name: Next step\n"
        "        run: echo next\n"
    )

    def test_it_reads_the_whole_block_dedented_and_stops_at_the_next_step(self):
        step = read_step(self.WORKFLOW_TEXT)

        assert step.script == "set -euo pipefail\n\nif true; then\n  echo indented\nfi\n"
        assert step.bot == "someone[bot]"

    def test_a_comment_at_a_shallow_indent_does_not_end_the_step(self):
        text = self.WORKFLOW_TEXT.replace(
            "        run: |", "# A note between keys.\n        run: |"
        )

        assert read_step(text) == read_step(self.WORKFLOW_TEXT)

    def test_a_missing_step_fails_rather_than_reading_nothing(self):
        with pytest.raises(AssertionError, match="0 steps named"):
            read_step(self.WORKFLOW_TEXT.replace(STEP, "Renamed"))

    def test_a_run_line_it_cannot_read_fails_rather_than_reading_nothing(self):
        with pytest.raises(AssertionError, match=re.escape("0 readable `run: |` blocks")):
            read_step(self.WORKFLOW_TEXT.replace("run: |\n          set", "run: >-\n          set"))
