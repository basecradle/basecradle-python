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
from basecradle._sessions import AsyncSessionsResource, Session, SessionsResource
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

#: "The caller did not pass this", distinct from an explicit ``None``. Typed ``Any`` so a
#: parameter can keep its honest ``str | None`` annotation while defaulting to the sentinel.
_UNSET: Any = object()

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
        #: The credential ``login()`` minted, as a ``Session`` — so a peer can revoke
        #: exactly what it created. ``None`` on a client built from an already-minted
        #: token: find that one in ``bc.sessions``, where ``current`` is ``True``.
        self.session: Session | None = None

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

    @staticmethod
    def _password_payload(
        current_password: str, password: str, password_confirmation: str | None
    ) -> dict[str, str]:
        """The body ``PATCH /users/password`` takes — the API requires all three fields.

        An *omitted* ``password_confirmation`` falls back to ``password``. The confirmation
        field exists to catch a human mistyping a new password into a second box; a caller
        handing the same string to two keyword arguments is not that check. Passing it is
        for when you *do* have a separate second entry to verify — then a difference
        raises ``PasswordConfirmationMismatchError`` instead of being papered over here.

        An explicit ``None`` is neither, so it raises: it means a caller wired up a second
        entry (``password_confirmation=form.get("confirm")``) and got nothing back. Falling
        back to ``password`` there would quietly change the password with no confirmation
        at all — the one case the argument exists to prevent.
        """
        if password_confirmation is _UNSET:
            confirmation = password
        elif password_confirmation is None:
            raise TypeError(
                "password_confirmation must be a string. Omit it entirely to confirm with "
                "`password` itself; passing None means a second entry was expected and did "
                "not arrive, which is never a confirmation."
            )
        else:
            confirmation = password_confirmation
        return {
            "current_password": current_password,
            "password": password,
            "password_confirmation": confirmation,
        }

    @classmethod
    def _client_from_login(
        cls, body: dict[str, Any], *, base_url: str, timeout: float, max_retries: int
    ) -> Any:
        """The shared, I/O-free tail of ``login()``: the client the minted token belongs to.

        ``POST /session`` returns the new credential as ``session`` — the same full shape
        ``GET /users/sessions`` lists — so the client carries it as a ``Session`` and the
        caller can revoke precisely the credential it just minted.
        """
        client = cls(
            token=body["token"], base_url=base_url, timeout=timeout, max_retries=max_retries
        )
        client.start_here = body.get("start_here")
        client.session = Session(body["session"], client=client)
        return client


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
        retrievable again. The credential itself is on the client as ``.session``, a full
        ``Session``, so you can revoke exactly what you minted (``bc.session.revoke()``).
        ``name`` is an optional label to tell credentials apart later. ``max_retries`` is
        carried onto the returned client (see ``BaseCradle``).
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

        return cls._client_from_login(
            response.json(), base_url=base_url, timeout=timeout, max_retries=max_retries
        )

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

    def change_password(
        self,
        *,
        current_password: str,
        password: str,
        password_confirmation: str | None = _UNSET,
    ) -> None:
        """Change this account's password (``PATCH /users/password``). Returns ``None``.

        Proving you hold the current password is what authorizes the change — the token
        alone is not enough.

        ``password_confirmation`` is optional: omit it and the new password confirms
        itself, which is what a caller holding one string should do. Pass it when you have
        a genuinely separate second entry, and a difference raises
        ``PasswordConfirmationMismatchError`` rather than going through. Passing an
        explicit ``None`` raises ``TypeError`` — it means a second entry was expected and
        never arrived, which is not a confirmation.

        .. note::
            **A password change signs nothing out.** Every session stays valid — this
            client's token, your other API tokens, and every web sign-in. So changing a
            password is not remediation for a leaked credential: revoke it
            (``session.revoke()``, or ``bc.sessions.revoke_all()`` for all of them).

        .. warning::
            A lost response leaves the outcome unknown. This is not a replayable request,
            so ``max_retries`` never re-sends it: if it fails with ``APIConnectionError``
            the change may still have landed, and retrying with the same arguments would
            then raise ``CurrentPasswordIncorrectError`` because the old password is gone.
            Settle it by trying to sign in, not by guessing.

        Raises ``CurrentPasswordIncorrectError`` if ``current_password`` is wrong and
        ``PasswordConfirmationMismatchError`` if the confirmation differs; a new password
        that fails the platform's strength rules raises ``ValidationError``.

        With ``AsyncBaseCradle``, await this: ``await abc.change_password(...)``.
        """
        self.request(
            "PATCH",
            "/users/password",
            json=self._password_payload(current_password, password, password_confirmation),
        )

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
        """Mint a fresh token via ``POST /session``, awaited. See ``BaseCradle.login``.

        The returned client carries the same ``.token``, ``.session`` and ``.start_here``;
        revoking the credential it minted is ``await client.session.revoke()``.
        """
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

        return cls._client_from_login(
            response.json(), base_url=base_url, timeout=timeout, max_retries=max_retries
        )

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

    async def change_password(
        self,
        *,
        current_password: str,
        password: str,
        password_confirmation: str | None = _UNSET,
    ) -> None:
        """Change this account's password, awaited. See ``BaseCradle.change_password``.

        The awaited twin: same arguments, same typed errors, same ``None`` return.
        It too signs nothing out — every session stays valid, so this is not remediation
        for a leaked credential. Revoke it: ``await session.revoke()``, or
        ``await abc.sessions.revoke_all()``.
        """
        await self.request(
            "PATCH",
            "/users/password",
            json=self._password_payload(current_password, password, password_confirmation),
        )

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
