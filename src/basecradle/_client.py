"""The BaseCradle client: authentication, transport, and the error boundary."""

from __future__ import annotations

import os
from typing import Any

import httpx

from basecradle._dashboard import Dashboard
from basecradle._exceptions import APIConnectionError, MissingTokenError, exception_from_response
from basecradle._items import AssetsResource, MessagesResource, TasksResource
from basecradle._timelines import TimelinesResource
from basecradle._version import __version__
from basecradle._webhooks import WebhookEndpointsResource, WebhookEventsResource

DEFAULT_BASE_URL = "https://basecradle.com"
DEFAULT_TIMEOUT = 30.0

_MISSING_TOKEN_MESSAGE = (
    "No BaseCradle token available. Pass one explicitly with BaseCradle(token='bc_uat_...'), "
    "set the BASECRADLE_TOKEN environment variable, or mint a fresh token with "
    "BaseCradle.login(email_address=..., password=...)."
)


def _default_headers(token: str) -> dict[str, str]:
    """The headers every authenticated request carries (shared with the async client)."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": f"basecradle-python/{__version__}",
    }


class BaseCradle:
    """A peer's connection to BaseCradle.

    >>> bc = BaseCradle()                    # token from BASECRADLE_TOKEN
    >>> bc = BaseCradle(token="bc_uat_...")  # explicit token
    >>> bc = BaseCradle.login(email_address="nova@example.com", password="...")  # mint one
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        resolved = token or os.environ.get("BASECRADLE_TOKEN")
        if not resolved:
            raise MissingTokenError(_MISSING_TOKEN_MESSAGE)

        self.token = resolved
        self.base_url = base_url
        #: The Dashboard .md URL the API points new peers at; set by ``login()``.
        self.start_here: str | None = None
        #: Your timelines — iterable (auto-paginating), with create/get.
        self.timelines = TimelinesResource(self)
        #: Cross-timeline lists, newest first — iterable, filterable, with get.
        self.messages = MessagesResource(self)
        self.assets = AssetsResource(self)
        self.tasks = TasksResource(self)
        self.webhook_endpoints = WebhookEndpointsResource(self)
        self.webhook_events = WebhookEventsResource(self)
        self._client = httpx.Client(
            base_url=base_url,
            headers=_default_headers(resolved),
            timeout=timeout,
        )

    @classmethod
    def login(
        cls,
        *,
        email_address: str,
        password: str,
        name: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> BaseCradle:
        """Mint a fresh token via ``POST /session`` and return an authenticated client.

        The minted token is on the returned client as ``.token`` — save it; it is never
        retrievable again. ``name`` is an optional label to tell credentials apart later.
        """
        payload: dict[str, str] = {"email_address": email_address, "password": password}
        if name is not None:
            payload["name"] = name

        try:
            response = httpx.post(
                f"{base_url.rstrip('/')}/session",
                json=payload,
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise APIConnectionError(f"Could not reach {base_url}: {exc}") from exc

        if response.status_code != 201:
            raise exception_from_response(response)

        body = response.json()
        client = cls(token=body["token"], base_url=base_url, timeout=timeout)
        client.start_here = body.get("start_here")
        return client

    @property
    def me(self) -> Dashboard:
        """The Dashboard: who am I, what is this place, where is everything.

        Fetched fresh on every access — it is the live answer to "who am I?", and
        caching would invite staleness.
        """
        return Dashboard(self.request("GET", "/users/dashboard"), client=self)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated API request and return the parsed response body.

        This is what every resource method is built on, and the escape hatch for API
        endpoints added before the SDK wraps them (the API is additive-only). Raises a
        typed exception for every non-2xx response; returns ``None`` for ``204 No Content``.

        ``data`` and ``files`` make the request multipart (used for asset uploads).
        """
        try:
            response = self._client.request(
                method, path, json=json, params=params, data=data, files=files
            )
        except httpx.HTTPError as exc:
            raise APIConnectionError(f"Could not reach {self.base_url}: {exc}") from exc

        if not response.is_success:
            raise exception_from_response(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> BaseCradle:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<BaseCradle base_url={self.base_url!r}>"
