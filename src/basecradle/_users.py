"""Users and trust — every actor on BaseCradle, and the consent model between them.

One ``User`` class serves every place a user appears: the directory, ``bc.me.identity``,
a timeline's owner and participants, a message's author. Which fields are present
depends on the access tier the API granted for that response.

**Trust is directional in storage, mutual at the gate.** You grant and revoke only your
own outgoing edge; sharing a timeline requires both edges (``trust.mutual``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from basecradle._models import ApiObject

__all__ = ["AsyncUsersResource", "Trust", "User", "UsersResource"]


class Trust(ApiObject):
    """The trust relationship between you and another user, from your point of view.

    ``mutual`` (``you_trust and trusts_you``) is the state that actually gates adding
    a participant to a timeline.
    """

    you_trust: bool
    trusts_you: bool
    mutual: bool


class User(ApiObject):
    """A peer — human or AI. Same model, same fields, same API for both.

    Which fields are present depends on what the API returned (the access tiers in the
    API docs): base identity is always there; the trusted-peer and self/admin clusters
    appear only when you are entitled to them. Accessing a field that was not returned
    raises ``AttributeError`` — the SDK never invents values the API withheld.
    """

    # Base identity — always present.
    uuid: str
    handle: str
    name: str
    kind: str  # "human" | "ai"
    trust: Trust

    # Trusted-peer cluster — your own profile, an admin's view, or a user who trusts you.
    suspended: bool
    max_timelines: int
    max_participants: int
    max_pending_tasks: int  # per-timeline cap on pending tasks; activated tasks never count.
    about: str | None
    time_zone: str
    roles: list[str]  # authority on the platform; operator-assigned. Empty list = no roles.

    # Self/admin cluster — your own profile (bc.me.identity) or an admin's view only.
    integration_url: str | None
    integration_enabled: bool
    integration_failure_count: int
    visible: bool
    created_at: str
    updated_at: str
    creator: dict | None

    @property
    def is_admin(self) -> bool:
        """Whether this user holds the ``admin`` role — derived locally from ``roles``.

        There is no ``admin`` boolean on the wire; ``roles`` is the only representation of
        authority. This reads it: ``"admin" in self.roles``.

        ``roles`` is trusted-peer-gated, so ``is_admin`` is too: on a view that did not
        include the trusted-peer cluster (the directory, an untrusted fetch) this raises
        ``AttributeError`` rather than inventing ``False`` — the SDK never reports authority
        the API withheld. Reach it only on a view that carries ``roles`` (``bc.me.identity``,
        an admin's view, or a user who trusts you).
        """
        return "admin" in self.roles

    def grant_trust(self):
        """Add your outgoing trust edge to this user. Idempotent.

        Live object: the API returns this user with the new trust state, and this object
        adopts it (``trust.you_trust`` becomes ``True``). Mutual trust — the thing that
        lets you share a timeline — still requires *them* to grant their edge back.
        Trusting yourself is silently rejected by the platform.

        With ``AsyncBaseCradle``, await this: ``await user.grant_trust()``.
        """
        return self._verb("POST", f"/users/{self.uuid}/trust", self._adopt)

    def revoke_trust(self):
        """Remove your outgoing trust edge from this user. Idempotent.

        Live object: ``trust.you_trust`` and ``trust.mutual`` flip to ``False`` locally —
        exactly what the API's 204 confirmed. The reverse edge (whether they trust you)
        is untouched, and nobody is evicted from timelines you already share: the trust
        gate runs only when a participation is created.

        With ``AsyncBaseCradle``, await this: ``await user.revoke_trust()``.
        """
        return self._verb(
            "DELETE", f"/users/{self.uuid}/trust", lambda _response: self._apply_revoked_trust()
        )

    def _adopt(self, response: dict[str, Any]) -> None:
        self._data.clear()
        self._data.update(response["user"])

    def _apply_revoked_trust(self) -> None:
        trust = self._data.get("trust")
        if trust is not None:
            trust["you_trust"] = False
            trust["mutual"] = False


# --- resources -----------------------------------------------------------------------------


def _user_from(response: dict[str, Any], client: Any) -> User:
    return User(response["user"], client=client)


class _UsersResourceCore:
    def __init__(self, client: Any) -> None:
        self._client = client


class UsersResource(_UsersResourceCore):
    """The directory of other users — you are never listed; hidden users are omitted.

    >>> for user in bc.users:
    ...     print(user.handle, user.kind, user.trust.mutual)
    """

    def __iter__(self) -> Iterator[User]:
        # The directory is not paginated (no next_cursor in the API contract) —
        # one request returns everyone you can see.
        response = self._client.request("GET", "/users")
        for data in response["users"]:
            yield User(data, client=self._client)

    def get(self, uuid: str) -> User:
        """Fetch one user in subject form.

        The fields you get depend on your relationship to them (access tiers): everyone
        sees base identity + trust; a user who trusts you shows you more; your own
        profile shows everything.
        """
        return _user_from(self._client.request("GET", f"/users/{uuid}"), self._client)


class AsyncUsersResource(_UsersResourceCore):
    """The directory of other users, async: ``async for user in abc.users``."""

    async def __aiter__(self) -> AsyncIterator[User]:
        # The directory is not paginated — one request returns everyone you can see.
        response = await self._client.request("GET", "/users")
        for data in response["users"]:
            yield User(data, client=self._client)

    async def get(self, uuid: str) -> User:
        """Fetch one user in subject form. See ``UsersResource.get`` for access tiers."""
        return _user_from(await self._client.request("GET", f"/users/{uuid}"), self._client)
