"""BaseCradle.login() — minting a token via POST /session."""

import json

import pytest

from basecradle import AccountSuspendedError, BaseCradle, InvalidCredentialsError
from tests.conftest import FAKE_TOKEN, problem

SESSION_RESPONSE = {
    "token": FAKE_TOKEN,
    "session": {"name": "api development", "created_at": "2026-01-01T00:00:00.000Z"},
    "start_here": "https://basecradle.com/users/dashboard.md",
}


class TestLogin:
    def test_successful_login(self, api):
        route = api.post("/session").respond(201, json=SESSION_RESPONSE)

        bc = BaseCradle.login(
            email_address="nova@example.com", password="correct-horse-battery-staple", name="nova"
        )

        assert bc.token == FAKE_TOKEN
        assert bc.start_here == "https://basecradle.com/users/dashboard.md"
        sent = json.loads(route.calls.last.request.read())
        assert sent == {
            "email_address": "nova@example.com",
            "password": "correct-horse-battery-staple",
            "name": "nova",
        }

    def test_login_request_is_unauthenticated(self, api):
        route = api.post("/session").respond(201, json=SESSION_RESPONSE)

        BaseCradle.login(email_address="nova@example.com", password="...")

        assert "Authorization" not in route.calls.last.request.headers

    def test_name_omitted_when_not_given(self, api):
        route = api.post("/session").respond(201, json=SESSION_RESPONSE)

        BaseCradle.login(email_address="john@example.com", password="...")

        sent = json.loads(route.calls.last.request.read())
        assert "name" not in sent

    def test_client_is_usable_after_login(self, api):
        api.post("/session").respond(201, json=SESSION_RESPONSE)
        dashboard_route = api.get("/users/dashboard").respond(200, json={"dashboard": {}})

        bc = BaseCradle.login(email_address="nova@example.com", password="...")
        bc.request("GET", "/users/dashboard")

        sent = dashboard_route.calls.last.request.headers
        assert sent["Authorization"] == f"Bearer {FAKE_TOKEN}"

    def test_invalid_credentials(self, api):
        api.post("/session").respond(
            401,
            json=problem(
                "invalid_credentials",
                401,
                detail="The email address or password is incorrect.",
            ),
        )

        with pytest.raises(InvalidCredentialsError) as exc_info:
            BaseCradle.login(email_address="john@example.com", password="wrong")

        assert exc_info.value.status == 401
        assert exc_info.value.code == "invalid_credentials"

    def test_suspended_account(self, api):
        api.post("/session").respond(
            403,
            json=problem(
                "account_suspended", 403, detail="This account is suspended and cannot sign in."
            ),
        )

        with pytest.raises(AccountSuspendedError):
            BaseCradle.login(email_address="john@example.com", password="...")
