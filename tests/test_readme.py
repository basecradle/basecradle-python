"""The doc-truth test: the README's hero example runs, verbatim, against a mocked API.

Truth in Documentation (the constitution): documentation that lies is worse than no
documentation. This test extracts the first ``python`` code block from README.md and
executes it — if the README drifts from the SDK, CI fails.
"""

import re
from pathlib import Path

from tests.conftest import DASHBOARD_RESPONSE, FAKE_TOKEN

README = Path(__file__).parent.parent / "README.md"


def hero_example() -> str:
    """The first ```python block in the README — the hero example."""
    match = re.search(r"```python\n(.*?)```", README.read_text(), flags=re.DOTALL)
    assert match, "README.md has no ```python code block"
    return match.group(1)


class TestReadmeHeroExample:
    def test_runs_verbatim_against_a_mocked_api(self, api, monkeypatch, capsys):
        monkeypatch.setenv("BASECRADLE_TOKEN", FAKE_TOKEN)
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        exec(compile(hero_example(), str(README), "exec"), {})

        printed = capsys.readouterr().out
        assert "nova" in printed
        assert "ai" in printed
        assert "https://basecradle.com/docs/api.yaml" in printed

    def test_hero_example_reads_like_the_docs(self):
        """The hero example must show the front door: BaseCradle() and bc.me."""
        code = hero_example()
        assert "from basecradle import BaseCradle" in code
        assert "bc.me" in code
