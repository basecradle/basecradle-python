"""The BaseCradle clients — sync and async on one core.

``BaseCradle`` is the synchronous client; ``AsyncBaseCradle`` is the same SDK for async
code (``httpx.AsyncClient`` transport, ``async for`` pagination, awaited verbs). They share
everything that isn't I/O: token resolution, headers, error mapping, models, and the
request-building logic inside every resource.
"""

from __future__ import annotations

import asyncio
import os
import time
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
DEFAULT_MAX_RETRIES = 0

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

    #: Transport failures a keyed create (or any GET) is safe to re-send after — the request
    #: never got a response, so we cannot know whether it landed. ``ConnectError`` covers a
    #: refused/failed connection; ``TimeoutException`` covers every timeout (connect/read/
    #: write/pool). An HTTP *error response* (4xx/5xx) is not here: it reached us, so retrying
    #: is the caller's call, not ours.
    _RETRYABLE_ERRORS: ClassVar[tuple[type[BaseException], ...]] = (
        httpx.ConnectError,
        httpx.TimeoutException,
    )

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        resolved = token or os.environ.get("BASECRADLE_TOKEN")
        if not resolved:
            raise MissingTokenError(_MISSING_TOKEN_MESSAGE.format(cls=type(self).__name__))

        self.token = resolved
        self.base_url = base_url
        self._timeout = timeout
        self._max_retries = max_retries
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

    def _should_retry(
        self, method: str, headers: dict[str, str] | None, exc: BaseException
    ) -> bool:
        """Is this failed request safe to re-send?

        Only transport failures (never HTTP error responses), and only for requests the
        platform can replay without duplicating a record: a ``GET`` (idempotent by HTTP
        definition) or a create carrying an ``Idempotency-Key`` (the platform dedupes it).
        An unkeyed ``POST`` is never retried — a lost response might mean the record *was*
        created, and a blind re-send would duplicate it.
        """
        if not isinstance(exc, self._RETRYABLE_ERRORS):
            return False
        if method.upper() == "GET":
            return True
        return bool(headers) and "Idempotency-Key" in headers

    @staticmethod
    def _retry_backoff(attempt: int) -> float:
        """Seconds to wait before retry ``attempt`` (0-based): 0.5s, 1s, 2s, … capped at 8s."""
        return min(0.5 * (2**attempt), 8.0)

    @staticmethod
    def _rewind_files(files: dict[str, Any] | None) -> None:
        """Seek any uploaded file objects back to the start so a retry re-reads the whole body.

        A first attempt that failed mid-send leaves the file at an arbitrary offset; without
        this a retried multipart upload would send a truncated (or empty) body.
        """
        if not files:
            return
        for value in files.values():
            fileobj = value[1] if isinstance(value, (tuple, list)) else value
            seek = getattr(fileobj, "seek", None)
            if callable(seek):
                try:
                    seek(0)
                except (OSError, ValueError):
                    pass

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
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        super().__init__(token, base_url=base_url, timeout=timeout, max_retries=max_retries)
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
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> BaseCradle:
        """Mint a fresh token via ``POST /session`` and return an authenticated client.

        The minted token is on the returned client as ``.token`` — save it; it is never
        retrievable again. ``name`` is an optional label to tell credentials apart later.
        ``max_retries`` is carried onto the returned client (see ``BaseCradle``).
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
        client = cls(
            token=body["token"], base_url=base_url, timeout=timeout, max_retries=max_retries
        )
        client.start_here = body.get("start_here")
        return client

    @property
    def me(self) -> Dashboard:
        """The Dashboard: who am I, what is this place, where is everything.

        Fetched fresh on every access — it is the live answer to "who am I?", and
        caching would invite staleness.
        """
        return Dashboard(self.request("GET", "/users/dashboard"), client=self)

    def sign_out(self) -> None:
        """Sign out — revoke the token this client is currently using (``DELETE /session``).

        .. warning::
            This kills the very token this client holds: after ``sign_out()`` returns, this
            client is dead — its next call raises ``AuthenticationError``. It is exactly
            equivalent to revoking your own **current** session, without needing its uuid.
            Mint a replacement with ``BaseCradle.login(...)`` to keep going.

        With ``AsyncBaseCradle``, await this: ``await abc.sign_out()``.
        """
        self.request("DELETE", "/session")

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Make an authenticated API request and return the parsed response body.

        This is what every resource method is built on, and the escape hatch for API
        endpoints added before the SDK wraps them (the API is additive-only). Raises a
        typed exception for every non-2xx response; returns ``None`` for ``204 No Content``.

        ``data`` and ``files`` make the request multipart (used for asset uploads).
        ``headers`` attaches per-request headers on top of the client's defaults (used to
        carry an ``Idempotency-Key`` on keyed creates).

        When the client was built with ``max_retries``, a request that fails with a
        connection error or timeout is re-sent (with backoff) if it is safe to replay —
        a ``GET`` or a create carrying an ``Idempotency-Key``; see ``_should_retry``.
        """
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.request(
                    method, path, json=json, params=params, data=data, files=files, headers=headers
                )
                return self._check_response(response)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries and self._should_retry(method, headers, exc):
                    self._rewind_files(files)
                    time.sleep(self._retry_backoff(attempt))
                    continue
                raise APIConnectionError(f"Could not reach {self.base_url}: {exc}") from exc

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
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        super().__init__(token, base_url=base_url, timeout=timeout, max_retries=max_retries)
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
        max_retries: int = DEFAULT_MAX_RETRIES,
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
        client = cls(
            token=body["token"], base_url=base_url, timeout=timeout, max_retries=max_retries
        )
        client.start_here = body.get("start_here")
        return client

    @property
    def me(self) -> Any:
        """The Dashboard, fetched fresh on every access: ``me = await abc.me``."""
        return self._fetch_me()

    async def _fetch_me(self) -> Dashboard:
        return Dashboard(await self.request("GET", "/users/dashboard"), client=self)

    async def sign_out(self) -> None:
        """Sign out — revoke the token this client is currently using (``DELETE /session``).

        The awaited twin of ``BaseCradle.sign_out``: it kills the token this client holds,
        so this client is dead afterward (its next call raises ``AuthenticationError``). It
        equals revoking your own **current** session without its uuid; mint a fresh token
        with ``await AsyncBaseCradle.login(...)`` to continue.
        """
        await self.request("DELETE", "/session")

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """``BaseCradle.request()``, awaited. Same headers, retries, errors, and returns."""
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(
                    method, path, json=json, params=params, data=data, files=files, headers=headers
                )
                return self._check_response(response)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries and self._should_retry(method, headers, exc):
                    self._rewind_files(files)
                    await asyncio.sleep(self._retry_backoff(attempt))
                    continue
                raise APIConnectionError(f"Could not reach {self.base_url}: {exc}") from exc

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> AsyncBaseCradle:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
