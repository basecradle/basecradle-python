"""Shared fixtures. All HTTP is mocked with respx — no test ever touches the network.

The fictional cast (per CLAUDE.md): John Doe (handle ``john``, human) and Nova Digital
(handle ``nova``, AI). Tokens are correctly-shaped fakes; UUIDs are well-formed UUIDv7.
"""

import pytest
import respx

from basecradle import BaseCradle

# bc_uat_ + 32 alphanumerics — the docs' own fabricated example token.
FAKE_TOKEN = "bc_uat_KqI8zFxkQ0OZ8vYwT7mWcVtR3nSdLpEa"

# A well-formed UUIDv7, used as the problem document's `instance`.
FAKE_INSTANCE = "019e7750-66ee-7f53-829f-13a8a710b6da"

BASE_URL = "https://basecradle.com"


@pytest.fixture
def token():
    return FAKE_TOKEN


@pytest.fixture
def bc(token):
    """An authenticated client pointed at the (mocked) production URL."""
    client = BaseCradle(token=token)
    yield client
    client.close()


@pytest.fixture
def api():
    """A respx router asserting that every mocked route actually gets called."""
    with respx.mock(base_url=BASE_URL, assert_all_called=True) as router:
        yield router


# The documented Dashboard example (docs → Dashboard), spec-complete: Nova Digital, an AI peer.
DASHBOARD_RESPONSE = {
    "you": {
        "uuid": "019e4b4c-3f21-7a90-b5e2-6c1f0a7d3e88",
        "handle": "nova",
        "name": "Nova Digital",
        "kind": "ai",
        "trust": {"you_trust": False, "trusts_you": False, "mutual": False},
        "suspended": False,
        "max_timelines": 15,
        "max_participants": 1,
        "about": None,
        "time_zone": "UTC",
        "integration_url": None,
        "integration_enabled": False,
        "integration_failure_count": 0,
        "visible": True,
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:00:00.000Z",
        "creator": None,
    },
    "environment": {
        "name": "BaseCradle",
        "summary": (
            "A communication platform and research lab where humans and AI are equal peers — "
            "same accounts, permissions, and API."
        ),
        "you_are": "a first-class peer here, not a tool.",
    },
    "interaction": {
        "timelines": {"url": "https://basecradle.com/timelines.json", "count": 3},
        "assets_url": "https://basecradle.com/assets.json",
        "messages_url": "https://basecradle.com/messages.json",
        "tasks_url": "https://basecradle.com/tasks.json",
        "webhook_endpoints_url": "https://basecradle.com/webhook_endpoints.json",
        "webhook_events_url": "https://basecradle.com/webhook_events.json",
    },
    "account": {
        "profile_url": "https://basecradle.com/users/019e4b4c-3f21-7a90-b5e2-6c1f0a7d3e88.json",
        "sessions_url": "https://basecradle.com/users/sessions.json",
        "change_password_url": "https://basecradle.com/users/password/edit",
    },
    "documentation": {
        "user_guide": "https://basecradle.com/docs/user_guide.md",
        "api": "https://basecradle.com/docs/api.md",
        "openapi": "https://basecradle.com/docs/api.yaml",
        "reference": "https://basecradle.com/docs/api/reference",
        "sdk": None,
    },
}


def problem(code, status, *, detail=None, title=None, **extra):
    """Build a problem+json document the way the API does."""
    body = {
        "type": f"https://basecradle.com/docs/api#error-{code}",
        "title": title or code.replace("_", " ").title(),
        "status": status,
        "code": code,
        "detail": detail or f"Fabricated detail for {code}.",
        "instance": FAKE_INSTANCE,
        **extra,
    }
    return body
