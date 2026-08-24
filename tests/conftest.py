"""Shared fixtures. All HTTP is mocked with respx — no test ever touches the network.

The fictional cast (per CLAUDE.md): John Doe (handle ``john``, human) and Nova Digital
(handle ``nova``, AI). Tokens are correctly-shaped fakes; UUIDs are well-formed UUIDv7.
"""

import pytest
import respx

from basecradle import AsyncBaseCradle, BaseCradle

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
    """An authenticated sync client pointed at the (mocked) production URL."""
    client = BaseCradle(token=token)
    yield client
    client.close()


@pytest.fixture
def anyio_backend():
    """Async tests run on asyncio (the anyio pytest plugin ships with httpx's deps)."""
    return "asyncio"


@pytest.fixture
async def abc(token):
    """An authenticated async client pointed at the (mocked) production URL."""
    client = AsyncBaseCradle(token=token)
    yield client
    await client.aclose()


@pytest.fixture
def api():
    """A respx router asserting that every mocked route actually gets called."""
    with respx.mock(base_url=BASE_URL, assert_all_called=True) as router:
        yield router


# The fictional cast, in nested-actor form (uuid, handle, name, kind — always these four).
JOHN = {
    "uuid": "019e7750-66ee-7e50-9e54-3bf8c3d6a8f1",
    "handle": "john",
    "name": "John Doe",
    "kind": "human",
}
NOVA = {
    "uuid": "019e7750-66ee-79c8-ad8a-bbb6ea7c2bcc",
    "handle": "nova",
    "name": "Nova Digital",
    "kind": "ai",
}

TIMELINE_UUID = "019e7750-66ee-7f53-829f-13a8a710b6da"
MESSAGE_UUID = "019e7750-66ee-7c4f-bcdc-7c5d2eddc662"
ASSET_UUID = "019e7750-66ee-7327-bc25-f4e64b0b3a02"
TASK_UUID = "019e7750-66ee-7f8e-a8c5-2c2cf95e2c0b"

ASSET_FILE = {
    "filename": "report.pdf",
    "byte_size": 184320,
    "content_type": "application/pdf",
    "checksum": "Yp9p9C8m6Xv2qS1nKQ0r3w==",
    "url": (
        "https://basecradle.com/rails/active_storage/blobs/redirect/"
        "eyJfcmFpbHMiOnsiZGF0YSI6MSwicHVyIjoiYmxvYl9pZCJ9fQ==--"
        "abc123def456abc123def456abc123def456abc1/report.pdf"
    ),
}


def _item_payload(type_, content, *, user=None, timeline_uuid=TIMELINE_UUID):
    return {
        "type": type_,
        "created_at": "2026-01-02T00:00:00.000Z",
        "user": user or JOHN,
        "timeline": {"uuid": timeline_uuid},
        "content": content,
    }


def message_payload(*, uuid=MESSAGE_UUID, body="Hello from a peer.", **kwargs):
    """A message in subject form (the docs' documented example)."""
    return _item_payload("message", {"uuid": uuid, "body": body}, **kwargs)


def asset_payload(*, uuid=ASSET_UUID, description="Quarterly report", **kwargs):
    """An asset in subject form (the docs' documented example)."""
    content = {"uuid": uuid, "description": description, "file": dict(ASSET_FILE)}
    return _item_payload("asset", content, **kwargs)


def directory_user_payload(*, user=None, you_trust=False, trusts_you=False):
    """A lean directory row: base identity + trust only (the docs' documented example)."""
    base = dict(user or NOVA)
    base["trust"] = {
        "you_trust": you_trust,
        "trusts_you": trusts_you,
        "mutual": you_trust and trusts_you,
    }
    return base


def trusted_peer_user_payload(*, user=None, roles=None, **trust):
    """The middle access tier: a user who trusts you (adds the trusted-peer cluster)."""
    return {
        **directory_user_payload(user=user, **trust),
        "suspended": False,
        "max_timelines": 15,
        "max_participants": 1,
        "max_pending_tasks": 3,
        "about": "Building things at BaseCradle.",
        "time_zone": "UTC",
        "roles": [] if roles is None else roles,
    }


API_SESSION_UUID = "019e84e4-9c0d-76a1-be70-0296c897b10b"
WEB_SESSION_UUID = "019e84e4-9c0d-7170-abf1-69869d3ca827"


def session_payload(
    *,
    uuid=API_SESSION_UUID,
    name="production agent",
    kind="api",
    current=True,
    last_used_at="2026-01-02T12:00:00.000Z",
):
    """A session row (the docs' documented example)."""
    return {
        "uuid": uuid,
        "name": name,
        "ip_address": "203.0.113.10" if kind == "api" else "198.51.100.7",
        "user_agent": "python-httpx/0.27.0" if kind == "api" else "Mozilla/5.0 (Macintosh)",
        "created_at": "2026-01-02T00:00:00.000Z",
        "last_used_at": last_used_at,
        "kind": kind,
        "current": current,
    }


WEBHOOK_ENDPOINT_UUID = "019e7750-66ee-79fc-a07f-0301cf1ace97"
WEBHOOK_EVENT_UUID = "019e7750-66ee-7ab2-b3a1-e1b87de9d3b6"
INGEST_URL = "https://basecradle.com/webhooks/019e7750-66ee-705a-803c-b25c5ee9b1f3"


def webhook_endpoint_payload(
    *,
    uuid=WEBHOOK_ENDPOINT_UUID,
    description="CI deploys",
    enabled=True,
    ingest_url=INGEST_URL,
    timeline_uuid=TIMELINE_UUID,
):
    """A webhook endpoint in subject form (the docs' documented example). No user block."""
    return {
        "type": "webhook_endpoint",
        "created_at": "2026-01-02T00:00:00.000Z",
        "timeline": {"uuid": timeline_uuid},
        "content": {
            "uuid": uuid,
            "description": description,
            "enabled": enabled,
            "ingest_url": ingest_url,
            "verification": {
                "enabled": False,
                "signature_header": "X-Signature",
                "verifier": "hmac_sha256_hex",
            },
        },
    }


def webhook_event_payload(
    *,
    uuid=WEBHOOK_EVENT_UUID,
    endpoint_uuid=WEBHOOK_ENDPOINT_UUID,
    timeline_uuid=TIMELINE_UUID,
    payload='{"status":"ok"}',
):
    """A webhook event in subject form (the docs' documented example). No user block."""
    return {
        "type": "webhook_event",
        "created_at": "2026-01-02T00:00:00.000Z",
        "timeline": {"uuid": timeline_uuid},
        "webhook_endpoint": {"uuid": endpoint_uuid},
        "content": {
            "uuid": uuid,
            "content_type": "application/json",
            "headers": {"HTTP_X_EXAMPLE_EVENT": "ping"},
            "payload": payload,
            "ingest_token_at_receipt": "019e7750-66ee-705a-803c-b25c5ee9b1f3",
        },
    }


def task_payload(
    *,
    uuid=TASK_UUID,
    instructions="Summarize the thread",
    activate_at="2026-01-03T15:00:00.000Z",
    status="pending",
    **kwargs,
):
    """A task in subject form (the docs' documented example)."""
    content = {
        "uuid": uuid,
        "instructions": instructions,
        "activate_at": activate_at,
        "status": status,
    }
    return _item_payload("task", content, **kwargs)


def timeline_payload(*, uuid=TIMELINE_UUID, name="Incident response", locked=False, **overrides):
    """A timeline in subject form (the docs' documented example), without items."""
    payload = {
        "uuid": uuid,
        "name": name,
        "locked": locked,
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-02T00:00:00.000Z",
        "owner": JOHN,
        "participants": [NOVA],
    }
    payload.update(overrides)
    return payload


# The documented Dashboard example (docs → Dashboard), spec-complete: Nova Digital, an AI peer.
DASHBOARD_RESPONSE = {
    "identity": {
        "uuid": "019e4b4c-3f21-7a90-b5e2-6c1f0a7d3e88",
        "handle": "nova",
        "name": "Nova Digital",
        "kind": "ai",
        # Reflexive self-trust: on your own subject form, trust is axiomatically all-true.
        "trust": {"you_trust": True, "trusts_you": True, "mutual": True},
        "suspended": False,
        "max_timelines": 15,
        "max_participants": 1,
        "max_pending_tasks": 3,
        "about": None,
        "time_zone": "UTC",
        "roles": [],
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
            "An AI Research Lab and Modular Agentic Framework where humans and AI are equal peers — "
            "same accounts, same permissions, same API."
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
        "changelog": "https://basecradle.com/docs/changelog.md",
        "openapi": "https://basecradle.com/docs/api.yaml",
        "reference": "https://basecradle.com/docs/api/reference",
        "sdks": {
            "python": {
                "repository": "https://github.com/basecradle/basecradle-python",
                "package": "https://pypi.org/project/basecradle/",
            },
        },
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
