"""AsyncBaseCradle: the same client, awaited — construction, auth, transport, errors."""

import json

import httpx
import pytest

import basecradle
from basecradle import (
    AccountSuspendedError,
    APIConnectionError,
    AsyncBaseCradle,
    Dashboard,
    InvalidCredentialsError,
    MissingTokenError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
    User,
    ValidationError,
)
from tests.conftest import DASHBOARD_RESPONSE, FAKE_TOKEN, problem

pytestmark = pytest.mark.anyio

SESSION_RESPONSE = {
    "token": FAKE_TOKEN,
    "session": {"name": "api development", "created_at": "2026-01-01T00:00:00.000Z"},
    "start_here": "https://basecradle.com/users/dashboard.md",
}


class TestConstruction:
    async def test_explicit_token(self):
        client = AsyncBaseCradle(token=FAKE_TOKEN)
        assert client.token == FAKE_TOKEN
        await client.aclose()

    async def test_token_from_environment(self, monkeypatch):
        monkeypatch.setenv("BASECRADLE_TOKEN", FAKE_TOKEN)
        client = AsyncBaseCradle()
        assert client.token == FAKE_TOKEN
        await client.aclose()

    async def test_missing_token_raises_with_async_guidance(self, monkeypatch):
        monkeypatch.delenv("BASECRADLE_TOKEN", raising=False)
        with pytest.raises(MissingTokenError) as exc_info:
            AsyncBaseCradle()
        message = str(exc_info.value)
        assert "BASECRADLE_TOKEN" in message
        assert "AsyncBaseCradle.login" in message  # the message names the right class

    async def test_async_context_manager(self):
        async with AsyncBaseCradle(token=FAKE_TOKEN) as client:
            assert isinstance(client, AsyncBaseCradle)
        assert client._client.is_closed

    async def test_repr_does_not_leak_token(self):
        client = AsyncBaseCradle(token=FAKE_TOKEN)
        assert FAKE_TOKEN not in repr(client)
        assert "AsyncBaseCradle" in repr(client)
        await client.aclose()


class TestHeaders:
    async def test_request_headers(self, abc, api):
        route = api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        await abc.request("GET", "/users/dashboard")

        sent = route.calls.last.request.headers
        assert sent["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert sent["Accept"] == "application/json"
        assert sent["User-Agent"] == f"basecradle-python/{basecradle.__version__}"


class TestLogin:
    async def test_successful_login(self, api):
        route = api.post("/session").respond(201, json=SESSION_RESPONSE)

        client = await AsyncBaseCradle.login(
            email_address="nova@example.com", password="correct-horse-battery-staple", name="nova"
        )

        assert client.token == FAKE_TOKEN
        assert client.start_here == "https://basecradle.com/users/dashboard.md"
        sent = json.loads(route.calls.last.request.read())
        assert sent == {
            "email_address": "nova@example.com",
            "password": "correct-horse-battery-staple",
            "name": "nova",
        }
        await client.aclose()

    async def test_invalid_credentials(self, api):
        api.post("/session").respond(401, json=problem("invalid_credentials", 401))

        with pytest.raises(InvalidCredentialsError):
            await AsyncBaseCradle.login(email_address="john@example.com", password="wrong")

    async def test_suspended_account(self, api):
        api.post("/session").respond(403, json=problem("account_suspended", 403))

        with pytest.raises(AccountSuspendedError):
            await AsyncBaseCradle.login(email_address="john@example.com", password="...")


class TestMe:
    async def test_me_is_awaited(self, abc, api):
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        me = await abc.me

        assert isinstance(me, Dashboard)
        assert isinstance(me.identity, User)
        assert me.identity.handle == "nova"

    async def test_me_is_fetched_fresh_on_every_access(self, abc, api):
        route = api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        await abc.me
        await abc.me

        assert route.call_count == 2


class TestErrors:
    """The error mapping is shared with the sync client — pin that it raises through async."""

    @pytest.mark.parametrize(
        ("code", "status", "error"),
        [
            ("unauthorized", 401, UnauthorizedError),
            ("not_found", 404, NotFoundError),
            ("validation_failed", 422, ValidationError),
            ("rate_limited", 429, RateLimitedError),
        ],
    )
    async def test_typed_errors_raise_through_async(self, abc, api, code, status, error):
        api.get("/users/dashboard").respond(status, json=problem(code, status))

        with pytest.raises(error) as exc_info:
            await abc.request("GET", "/users/dashboard")

        assert exc_info.value.code == code

    async def test_retry_after_extraction(self, abc, api):
        api.get("/users/dashboard").respond(
            429, json=problem("rate_limited", 429), headers={"Retry-After": "42"}
        )

        with pytest.raises(RateLimitedError) as exc_info:
            await abc.request("GET", "/users/dashboard")

        assert exc_info.value.retry_after == 42

    async def test_transport_error_wrapped(self, abc, api):
        api.get("/users/dashboard").mock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(APIConnectionError) as exc_info:
            await abc.request("GET", "/users/dashboard")

        assert isinstance(exc_info.value.__cause__, httpx.ConnectError)

    async def test_204_returns_none(self, abc, api):
        api.delete("/users/sessions").respond(204)

        assert await abc.request("DELETE", "/users/sessions") is None
