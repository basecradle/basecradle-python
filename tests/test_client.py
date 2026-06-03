"""Construction, headers, transport, and the request() error boundary."""

import httpx
import pytest
import respx

import basecradle
from basecradle import APIConnectionError, BaseCradle, MissingTokenError, NotFoundError
from tests.conftest import BASE_URL, FAKE_TOKEN, problem


class TestConstruction:
    def test_explicit_token(self):
        bc = BaseCradle(token=FAKE_TOKEN)
        assert bc.token == FAKE_TOKEN

    def test_token_from_environment(self, monkeypatch):
        monkeypatch.setenv("BASECRADLE_TOKEN", FAKE_TOKEN)
        bc = BaseCradle()
        assert bc.token == FAKE_TOKEN

    def test_explicit_token_beats_environment(self, monkeypatch):
        monkeypatch.setenv("BASECRADLE_TOKEN", "bc_uat_EnvVarTokenThatMustNotBeUsed00")
        bc = BaseCradle(token=FAKE_TOKEN)
        assert bc.token == FAKE_TOKEN

    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.delenv("BASECRADLE_TOKEN", raising=False)
        with pytest.raises(MissingTokenError) as exc_info:
            BaseCradle()
        # The message must say exactly how to fix it.
        message = str(exc_info.value)
        assert "BASECRADLE_TOKEN" in message
        assert "BaseCradle.login" in message

    def test_missing_token_is_catchable_as_basecradle_error(self, monkeypatch):
        monkeypatch.delenv("BASECRADLE_TOKEN", raising=False)
        with pytest.raises(basecradle.BaseCradleError):
            BaseCradle()

    def test_start_here_is_none_without_login(self):
        bc = BaseCradle(token=FAKE_TOKEN)
        assert bc.start_here is None


class TestHeaders:
    def test_request_headers(self, bc, api):
        route = api.get("/users/dashboard").respond(200, json={"dashboard": {}})

        bc.request("GET", "/users/dashboard")

        sent = route.calls.last.request.headers
        assert sent["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert sent["Accept"] == "application/json"
        assert sent["User-Agent"] == f"basecradle-python/{basecradle.__version__}"

    def test_user_agent_carries_current_version(self, bc, api):
        api.get("/users/dashboard").respond(200, json={"dashboard": {}})

        bc.request("GET", "/users/dashboard")

        sent = api.calls.last.request.headers
        assert basecradle.__version__ in sent["User-Agent"]


class TestBaseUrl:
    @respx.mock(base_url="http://localhost:3000", assert_all_called=True)
    def test_base_url_override(self, respx_mock):
        respx_mock.get("/users/dashboard").respond(200, json={"dashboard": {}})

        bc = BaseCradle(token=FAKE_TOKEN, base_url="http://localhost:3000")
        assert bc.request("GET", "/users/dashboard") == {"dashboard": {}}


class TestRequest:
    def test_returns_parsed_envelope(self, bc, api):
        timeline = {"uuid": "019e7750-66ee-7f53-829f-13a8a710b6da", "name": "Incident response"}
        api.get("/timelines/019e7750-66ee-7f53-829f-13a8a710b6da").respond(
            200, json={"timeline": timeline}
        )

        body = bc.request("GET", "/timelines/019e7750-66ee-7f53-829f-13a8a710b6da")

        assert body == {"timeline": timeline}

    def test_204_returns_none(self, bc, api):
        api.delete("/users/sessions/019e84e4-9c0d-76a1-be70-0296c897b10b").respond(204)

        result = bc.request("DELETE", "/users/sessions/019e84e4-9c0d-76a1-be70-0296c897b10b")

        assert result is None

    def test_json_body_and_params_are_sent(self, bc, api):
        import json as jsonlib

        route = api.post("/timelines", params={"foo": "bar"}).respond(
            201, json={"timeline": {"name": "Incident response"}}
        )

        bc.request("POST", "/timelines", json={"name": "Incident response"}, params={"foo": "bar"})

        sent = jsonlib.loads(route.calls.last.request.read())
        assert sent == {"name": "Incident response"}

    def test_non_2xx_raises_typed_error(self, bc, api):
        api.get("/timelines/019e7750-66ee-7f53-829f-13a8a710b6da").respond(
            404, json=problem("not_found", 404)
        )

        with pytest.raises(NotFoundError):
            bc.request("GET", "/timelines/019e7750-66ee-7f53-829f-13a8a710b6da")


class TestConnectionErrors:
    def test_transport_error_wrapped(self, bc, api):
        api.get("/users/dashboard").mock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(APIConnectionError) as exc_info:
            bc.request("GET", "/users/dashboard")

        assert isinstance(exc_info.value.__cause__, httpx.ConnectError)

    def test_timeout_wrapped(self, bc, api):
        api.get("/users/dashboard").mock(side_effect=httpx.ReadTimeout("timed out"))

        with pytest.raises(APIConnectionError):
            bc.request("GET", "/users/dashboard")


class TestLifecycle:
    def test_context_manager_closes(self):
        with BaseCradle(token=FAKE_TOKEN) as bc:
            assert isinstance(bc, BaseCradle)
        assert bc._client.is_closed

    def test_close(self):
        bc = BaseCradle(token=FAKE_TOKEN)
        bc.close()
        assert bc._client.is_closed

    def test_repr_does_not_leak_token(self):
        bc = BaseCradle(token=FAKE_TOKEN)
        assert FAKE_TOKEN not in repr(bc)
        assert BASE_URL in repr(bc)
