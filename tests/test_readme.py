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
    asset_payload,
    directory_user_payload,
    message_payload,
    session_payload,
    task_payload,
    timeline_payload,
    trusted_peer_user_payload,
    webhook_endpoint_payload,
    webhook_event_payload,
)

README = Path(__file__).parent.parent / "README.md"


def python_blocks() -> list[str]:
    """Every ```python block in the README, in order."""
    blocks = re.findall(r"```python\n(.*?)```", README.read_text(), flags=re.DOTALL)
    assert blocks, "README.md has no ```python code blocks"
    return blocks


def hero_block() -> str:
    """The 'Who am I?' front-door example, found by content rather than position
    so it survives sections (Authentication, …) being added above it."""
    matches = [b for b in python_blocks() if "bc.me" in b and "me.documentation.openapi" in b]
    assert len(matches) == 1, "expected exactly one hero (bc.me) block in README.md"
    return matches[0]


class TestReadmeExamples:
    @pytest.fixture(autouse=True)
    def mocked_platform(self, monkeypatch, tmp_path):
        """Enough mocked API surface for every README example to run against.

        Not every example calls every route, so this router does not assert-all-called.
        Examples referencing local files (./report.pdf) run in a temp directory where
        that file genuinely exists.
        """
        monkeypatch.setenv("BASECRADLE_TOKEN", FAKE_TOKEN)
        (tmp_path / "report.pdf").write_bytes(b"%PDF-1.7 fabricated example bytes")
        monkeypatch.chdir(tmp_path)

        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            router.post("/session").respond(
                201,
                json={
                    "token": FAKE_TOKEN,
                    "session": {"name": "Test from Python"},
                    "start_here": "https://basecradle.com/users/dashboard.md",
                },
            )
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
            router.delete(path__regex=r"/timelines/[^/]+$").respond(204)
            router.post(path__regex=r"/timelines/.+/messages$").respond(
                201, json={"message": message_payload()}
            )
            router.post(path__regex=r"/timelines/.+/assets$").respond(
                201, json={"asset": asset_payload()}
            )
            router.post(path__regex=r"/timelines/.+/tasks$").respond(
                201, json={"task": task_payload()}
            )
            router.post(path__regex=r"/tasks/.+/cancellation$").respond(
                200, json={"task": task_payload(status="cancelled")}
            )
            router.get("/messages").respond(
                200, json={"messages": [message_payload()], "next_cursor": None}
            )
            router.get("/assets").respond(
                200, json={"assets": [asset_payload()], "next_cursor": None}
            )
            router.get("/tasks").respond(200, json={"tasks": [task_payload()], "next_cursor": None})
            router.post(path__regex=r"/timelines/.+/webhook_endpoints$").respond(
                201, json={"webhook_endpoint": webhook_endpoint_payload()}
            )
            router.route(
                method__in=["POST", "DELETE"],
                path__regex=r"/webhook_endpoints/.+/enablement$",
            ).respond(200, json={"webhook_endpoint": webhook_endpoint_payload()})
            router.post(path__regex=r"/webhook_endpoints/.+/rotation$").respond(
                200, json={"webhook_endpoint": webhook_endpoint_payload()}
            )
            router.get("/webhook_events").respond(
                200, json={"webhook_events": [webhook_event_payload()], "next_cursor": None}
            )
            router.get("/users/sessions").respond(
                200,
                json={
                    "sessions": [
                        session_payload(current=True),
                        session_payload(
                            uuid="019e84e4-9c0d-7170-abf1-69869d3ca827",
                            name="stale ci runner",
                            current=False,
                        ),
                    ],
                    "next_cursor": None,
                },
            )
            router.delete(path__regex=r"/users/sessions/.+$").respond(204)
            router.get("/users").respond(
                200, json={"users": [directory_user_payload(user=NOVA, trusts_you=True)]}
            )
            router.get(path__regex=r"/users/019e.+$").respond(
                200, json={"user": directory_user_payload(user=NOVA, trusts_you=True)}
            )
            router.post(path__regex=r"/users/.+/trust$").respond(
                201,
                json={
                    "user": trusted_peer_user_payload(user=NOVA, you_trust=True, trusts_you=True)
                },
            )
            yield router

    @pytest.mark.parametrize("block_number", range(len(python_blocks())))
    def test_block_runs_verbatim(self, block_number):
        code = python_blocks()[block_number]

        exec(compile(code, f"{README}#block{block_number}", "exec"), {})

    def test_hero_example_prints_the_peer_identity(self, capsys):
        exec(compile(hero_block(), str(README), "exec"), {})

        printed = capsys.readouterr().out
        assert "nova" in printed
        assert "https://basecradle.com/docs/api.yaml" in printed

    def test_hero_example_shows_the_front_door(self):
        code = hero_block()
        assert "from basecradle import BaseCradle" in code
        assert "bc.me" in code
