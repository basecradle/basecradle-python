"""The doc-truth test: every Python example in the README runs, verbatim, against a mocked API.

Truth in Documentation (the constitution): documentation that lies is worse than no
documentation. This test extracts every ``python`` code block from README.md and
executes each one — if the README drifts from the SDK, CI fails.
"""

import re
from pathlib import Path

import pytest
import respx

from tests.conftest import (
    BASE_URL,
    DASHBOARD_RESPONSE,
    FAKE_TOKEN,
    NOVA,
    TIMELINE_UUID,
    timeline_payload,
)

README = Path(__file__).parent.parent / "README.md"


def python_blocks() -> list[str]:
    """Every ```python block in the README, in order."""
    blocks = re.findall(r"```python\n(.*?)```", README.read_text(), flags=re.DOTALL)
    assert blocks, "README.md has no ```python code blocks"
    return blocks


class TestReadmeExamples:
    @pytest.fixture(autouse=True)
    def mocked_platform(self, monkeypatch):
        """Enough mocked API surface for every README example to run against.

        Not every example calls every route, so this router does not assert-all-called.
        """
        monkeypatch.setenv("BASECRADLE_TOKEN", FAKE_TOKEN)
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            router.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)
            router.get("/timelines").respond(
                200, json={"timelines": [timeline_payload()], "next_cursor": None}
            )
            router.post("/timelines").respond(
                201, json={"timeline": timeline_payload(participants=[]), "items": []}
            )
            router.post(path__regex=r"/timelines/.+/participations$").respond(201, json=NOVA)
            router.post(path__regex=r"/timelines/.+/lock$").respond(
                200, json={"uuid": TIMELINE_UUID, "locked": True}
            )
            yield router

    @pytest.mark.parametrize("block_number", range(len(python_blocks())))
    def test_block_runs_verbatim(self, block_number):
        code = python_blocks()[block_number]

        exec(compile(code, f"{README}#block{block_number}", "exec"), {})

    def test_hero_example_prints_the_peer_identity(self, capsys):
        exec(compile(python_blocks()[0], str(README), "exec"), {})

        printed = capsys.readouterr().out
        assert "nova" in printed
        assert "https://basecradle.com/docs/api.yaml" in printed

    def test_hero_example_shows_the_front_door(self):
        code = python_blocks()[0]
        assert "from basecradle import BaseCradle" in code
        assert "bc.me" in code
