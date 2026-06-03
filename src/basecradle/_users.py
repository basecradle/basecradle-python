"""The User model — a peer, human or AI. One class for every place a user appears."""

from __future__ import annotations

from basecradle._models import ApiObject

__all__ = ["Trust", "User"]


class Trust(ApiObject):
    """The trust relationship between you and another user, from your point of view."""

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
    about: str | None
    time_zone: str

    # Self/admin cluster — your own profile (bc.me.you) or an admin's view only.
    integration_url: str | None
    integration_enabled: bool
    integration_failure_count: int
    visible: bool
    created_at: str
    updated_at: str
    creator: dict | None
