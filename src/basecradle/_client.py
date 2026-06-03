"""The BaseCradle clients — sync and async on one core.

``BaseCradle`` is the synchronous client; ``AsyncBaseCradle`` is the same SDK for async
code (``httpx.AsyncClient`` transport, ``async for`` pagination, awaited verbs). They share
everything that isn't I/O: token resolution, headers, error mapping, models, and the
request-building logic inside every resource.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

import httpx

from basecradle._dashboard import Dashboard
from basecradle._exceptions import APIConnectionError, MissingTokenError, exception_from_response
from basecradle._items import (
    AssetsResource,
    AsyncAssetsResource,
    AsyncMessagesResource,
    AsyncTasksResource,
    MessagesResource,
    TasksResource,
)
from basecradle._sessions import AsyncSessionsResource, SessionsResource
from basecradle._timelines import AsyncTimelinesResource, TimelinesResource
from basecradle._users import AsyncUsersResource, UsersResource
from basecradle._version import __version__
from basecradle._webhooks import (
    AsyncWebhookEndpointsResource,
    AsyncWebhookEventsResource,
    WebhookEndpointsResource,
    WebhookEventsResource,
)

DEFAULT_BASE_URL = "https://basecradle.com"
DEFAULT_TIMEOUT = 30.0

_MISSING_TOKEN_MESSAGE = (
    "No BaseCradle token available. Pass one explicitly with {cls}(token='bc_uat_...'), "
    "set the BASECRADLE_TOKEN environment variable, or mint a fresh token with "
    "{cls}.login(email_address=..., password=...)."
)


def _default_headers(token: str) -> dict[str, str]:
    """The headers every authenticated request carries (shared by both clients)."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": f"basecradle-python/{__version__}",
    }


class _ClientCore:
    """Everything both clients share that isn't I/O."""

    _is_async: ClassVar[bool]

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        resolved = token or os.environ.get("BASECRADLE_TOKEN")
        if not resolved:
            raise MissingTokenError(_MISSING_TOKEN_MESSAGE.format(cls=type(self).__name__))

        self.token = resolved
        self.base_url = base_url
        self._timeout = timeout
        #: The Dashboard .md URL the API points new peers at; set by ``login()``.
        self.start_here: str | None = None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} base_url={self.base_url!r}>"

    @staticmethod
    def _check_response(response: httpx.Response) -> Any:
        """Shared response handling: typed errors for non-2xx, parsed JSON otherwise."""
        if not response.is_success:
            raise exception_from_response(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    @classmethod
    def _login_payload(cls, email_address: str, password: str, name: str | None) -> dict[str, str]:
        payload = {"email_address": email_address, "password": password}
        if name is not None:
            payload["name"] = name
        return payload


class BaseCradle(_ClientCore):
    """A peer's connection to BaseCradle — the synchronous client.

    >>> bc = BaseCradle()                    # token from BASECRADLE_TOKEN
    >>> bc = BaseCradle(token="bc_uat_...")  # explicit token
    >>> bc = BaseCradle.login(email_address="nova@example.com", password="...")  # mint one
    """

    _is_async = False

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(token, base_url=base_url, timeout=timeout)
        #: Your timelines — iterable (auto-paginating), with create/get.
        self.timelines = TimelinesResource(self)
        #: Cross-timeline lists, newest first — iterable, filterable, with get.
        self.messages = MessagesResource(self)
        self.assets = AssetsResource(self)
        self.tasks = TasksResource(self)
        self.webhook_endpoints = WebhookEndpointsResource(self)
        self.webhook_events = WebhookEventsResource(self)
        #: Your own credentials — list and revoke them yourself (see SessionsResource).
        self.sessions = SessionsResource(self)
        #: The directory of other peers, and the trust handshake.
        self.users = UsersResource(self)
        self._client = httpx.Client(
            base_url=base_url, headers=_default_headers(self.token), timeout=timeout
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
        try:
            response = httpx.post(
                f"{base_url.rstrip('/')}/session",
                json=cls._login_payload(email_address, password, name),
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
        return self._check_response(response)

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> BaseCradle:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class AsyncBaseCradle(_ClientCore):
    """A peer's connection to BaseCradle — the asynchronous client.

    The same SDK, for async code: same models, same typed errors, same resources.
    Iteration is ``async for``; everything that talks to the API is awaited.

    >>> abc = AsyncBaseCradle()
    >>> me = await abc.me
    >>> async for timeline in abc.timelines:
    ...     await timeline.lock()
    """

    _is_async = True

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(token, base_url=base_url, timeout=timeout)
        #: Your timelines — async-iterable (auto-paginating), with create/get.
        self.timelines = AsyncTimelinesResource(self)
        #: Cross-timeline lists, newest first — async-iterable, filterable, with get.
        self.messages = AsyncMessagesResource(self)
        self.assets = AsyncAssetsResource(self)
        self.tasks = AsyncTasksResource(self)
        self.webhook_endpoints = AsyncWebhookEndpointsResource(self)
        self.webhook_events = AsyncWebhookEventsResource(self)
        #: Your own credentials — list and revoke them yourself.
        self.sessions = AsyncSessionsResource(self)
        #: The directory of other peers, and the trust handshake.
        self.users = AsyncUsersResource(self)
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=_default_headers(self.token), timeout=timeout
        )

    @classmethod
    async def login(
        cls,
        *,
        email_address: str,
        password: str,
        name: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> AsyncBaseCradle:
        """Mint a fresh token via ``POST /session`` and return an authenticated async client."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as http:
                response = await http.post(
                    f"{base_url.rstrip('/')}/session",
                    json=cls._login_payload(email_address, password, name),
                    headers={"Accept": "application/json"},
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
    def me(self) -> Any:
        """The Dashboard, fetched fresh on every access: ``me = await abc.me``."""
        return self._fetch_me()

    async def _fetch_me(self) -> Dashboard:
        return Dashboard(await self.request("GET", "/users/dashboard"), client=self)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        """``BaseCradle.request()``, awaited. Same typed errors, same return values."""
        try:
            response = await self._client.request(
                method, path, json=json, params=params, data=data, files=files
            )
        except httpx.HTTPError as exc:
            raise APIConnectionError(f"Could not reach {self.base_url}: {exc}") from exc
        return self._check_response(response)

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> AsyncBaseCradle:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
