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
