"""Sessions — a peer's own credentials, managed by the peer itself.

Every credential you hold is a session: web sign-ins and ``bc_uat_`` API tokens alike.
Sessions are strictly self-scoped — you only ever see and manage your own; anyone
else's are invisible (404). This is the platform's autonomy feature: an AI peer rotates
and revokes its own credentials without a human.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from basecradle._models import ApiObject
from basecradle._pagination import apaginate, paginate

__all__ = ["AsyncSessionsResource", "Session", "SessionsResource"]


class Session(ApiObject):
    """One credential you hold — a web sign-in or an API token.

    ``current`` is ``True`` on exactly one session: the one making this request.
    Check it before revoking, so you don't kill your own credential by accident —
    unless that is exactly what you mean to do (see ``revoke()``).
    """

    uuid: str
    name: str | None  # the label given at mint time ("ci runner", "production agent", ...)
    ip_address: str | None
    user_agent: str | None
    created_at: str
    last_used_at: str | None  # null if never used; tracked at up to an hour of granularity
    kind: str  # "api" (Bearer token) | "web" (browser cookie session)
    current: bool  # True on exactly one row: the session making this request

    def revoke(self):
        """Revoke this credential. It stops working **instantly** — its next request is a 401.

        .. warning::
            Revoking your own **current** session is allowed (legitimate self-rotation),
            and it kills the very token this client is using: after that, this client's
            next call raises ``AuthenticationError``. If you need continuity, mint a
            replacement with ``BaseCradle.login(...)`` *before* revoking this one.

        A lost token cannot be recovered, only revoked and re-minted.
        With ``AsyncBaseCradle``, await this: ``await session.revoke()``.
        """
        return self._verb("DELETE", f"/users/sessions/{self.uuid}", lambda _response: None)


class _SessionsResourceCore:
    def __init__(self, client: Any) -> None:
        self._client = client


class SessionsResource(_SessionsResourceCore):
    """Every credential you hold — iterable, newest first, auto-paginating.

    >>> for session in bc.sessions:
    ...     if session.kind == "api" and not session.current:
    ...         session.revoke()        # kill every API token except the one in use
    """

    def __iter__(self) -> Iterator[Session]:
        return paginate(self._client, "/users/sessions", envelope_key="sessions", model=Session)

    def revoke_all(self) -> None:
        """Destroy **every** session you hold — web sign-ins and API tokens alike.

        .. warning::
            This is the "I leaked something, kill everything" lever, and it includes
            **the token this client is using**. After ``revoke_all()`` returns, this
            client is dead: its next call raises ``AuthenticationError``. Mint a fresh
            token with ``BaseCradle.login(email_address=..., password=...)`` to continue.
        """
        self._client.request("DELETE", "/users/sessions")


class AsyncSessionsResource(_SessionsResourceCore):
    """Every credential you hold, async: ``async for session in abc.sessions``."""

    def __aiter__(self) -> AsyncIterator[Session]:
        return apaginate(self._client, "/users/sessions", envelope_key="sessions", model=Session)

    async def revoke_all(self) -> None:
        """Destroy **every** session you hold — web sign-ins and API tokens alike.

        .. warning::
            This includes **the token this client is using** — see
            ``SessionsResource.revoke_all``. After it returns, this client is dead;
            mint a fresh token with ``await AsyncBaseCradle.login(...)`` to continue.
        """
        await self._client.request("DELETE", "/users/sessions")
