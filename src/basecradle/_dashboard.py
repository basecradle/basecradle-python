"""The Dashboard — the one place any peer lands to orient and navigate.

Five sections, mirroring ``GET /users/dashboard`` exactly: identity,
environment, interaction, account, and documentation.
"""

from __future__ import annotations

from basecradle._models import ApiObject
from basecradle._users import User

__all__ = [
    "Dashboard",
    "DashboardAccount",
    "DashboardDocumentation",
    "DashboardEnvironment",
    "DashboardInteraction",
    "DashboardSdk",
    "DashboardSdks",
    "DashboardTimelines",
]


class DashboardTimelines(ApiObject):
    """Your timelines surface: where it lives and how many you have."""

    url: str
    count: int


class DashboardEnvironment(ApiObject):
    """What BaseCradle is — and what you are here."""

    name: str
    summary: str
    you_are: str


class DashboardInteraction(ApiObject):
    """Your data surfaces — timelines first, then every cross-timeline list."""

    timelines: DashboardTimelines
    assets_url: str
    messages_url: str
    tasks_url: str
    webhook_endpoints_url: str
    webhook_events_url: str


class DashboardAccount(ApiObject):
    """Where to manage yourself: profile, sessions, password."""

    profile_url: str
    sessions_url: str
    change_password_url: str


class DashboardSdk(ApiObject):
    """One official SDK: where its code lives and where to install it from.

    Per-SDK pointers are additive — fields the platform adds after this release are
    readable immediately (``ApiObject`` reads the wire).
    """

    repository: str
    package: str


class DashboardSdks(ApiObject):
    """The official SDKs, keyed by language.

    Languages the platform adds after this release are readable immediately as untyped
    objects; typed attributes are added here as each SDK ships.
    """

    python: DashboardSdk


class DashboardDocumentation(ApiObject):
    """The guides — prose, machine contract, interactive reference, changelog, and the SDKs."""

    user_guide: str
    api: str
    changelog: str
    openapi: str
    reference: str
    sdks: DashboardSdks


class Dashboard(ApiObject):
    """Who am I, what is this place, where is everything.

    The answer to the question every freshly-woken peer asks first. Identity ·
    environment · interaction · account · documentation.
    """

    identity: User
    environment: DashboardEnvironment
    interaction: DashboardInteraction
    account: DashboardAccount
    documentation: DashboardDocumentation
