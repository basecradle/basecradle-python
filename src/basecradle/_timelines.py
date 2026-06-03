"""Timelines — the container everything else lives on. One model, two clients."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from basecradle._items import (
    AsyncTimelineAssets,
    AsyncTimelineMessages,
    AsyncTimelineTasks,
    TimelineAssets,
    TimelineMessages,
    TimelineTasks,
    _uuid_of,
)
from basecradle._models import ApiObject
from basecradle._pagination import apaginate, paginate
from basecradle._users import User
from basecradle._webhooks import (
    AsyncTimelineWebhookEndpoints,
    AsyncTimelineWebhookEvents,
    TimelineWebhookEndpoints,
    TimelineWebhookEvents,
)

__all__ = ["AsyncTimelinesResource", "Timeline", "TimelineItem", "TimelinesResource"]


class TimelineItem(ApiObject):
    """One item on a timeline — a message, asset, or webhook event.

    ``type`` says which; ``content`` is the item itself, wire-exact (for a message:
    ``uuid`` and ``body``). ``user`` is the author in nested-actor form.
    """

    type: str  # "message" | "asset" | "webhook_event"
    created_at: str
    user: User
    content: ApiObject


class Timeline(ApiObject):
    """A timeline: its metadata, owner, participants, lock state — and its verbs.

    Verbs update this object with exactly what the API confirmed changed (live objects,
    Rails-style). With ``AsyncBaseCradle``, verbs return coroutines — await them:
    ``await timeline.lock()``.
    """

    uuid: str
    name: str
    locked: bool
    created_at: str
    updated_at: str
    owner: User
    participants: list[User]
    # Present when the timeline is the subject of the response (get / create).
    # List rows don't carry items — fetch the timeline to get them.
    items: list[TimelineItem]

    # -- nested resources (sync or async to match the attached client) --

    @property
    def messages(self) -> TimelineMessages | AsyncTimelineMessages:
        """This timeline's messages: ``.create(body=...)`` or iterate (newest first)."""
        return self._nested(TimelineMessages, AsyncTimelineMessages)

    @property
    def assets(self) -> TimelineAssets | AsyncTimelineAssets:
        """This timeline's assets: ``.create(file=...)`` (multipart) or iterate."""
        return self._nested(TimelineAssets, AsyncTimelineAssets)

    @property
    def tasks(self) -> TimelineTasks | AsyncTimelineTasks:
        """This timeline's tasks: ``.create(instructions=..., activate_at=...)`` or iterate."""
        return self._nested(TimelineTasks, AsyncTimelineTasks)

    @property
    def webhook_endpoints(self) -> TimelineWebhookEndpoints | AsyncTimelineWebhookEndpoints:
        """This timeline's inbound webhook endpoints: ``.create(description=...)`` or iterate."""
        return self._nested(TimelineWebhookEndpoints, AsyncTimelineWebhookEndpoints)

    @property
    def webhook_events(self) -> TimelineWebhookEvents | AsyncTimelineWebhookEvents:
        """This timeline's webhook events (read-only) — iterate, newest first."""
        return self._nested(TimelineWebhookEvents, AsyncTimelineWebhookEvents)

    def _nested(self, sync_class: type, async_class: type) -> Any:
        client = self._require_client()
        return (async_class if client._is_async else sync_class)(client, self.uuid)

    # -- verbs (await them with AsyncBaseCradle) --

    def lock(self):
        """The emergency stop: freeze the timeline's content, permanently.

        Any viewer can lock; locking is idempotent and one-way (unlocking is an
        out-of-band admin action). Participant management stays available so an owner
        can lock first, then remove whoever caused it.

        With ``AsyncBaseCradle``, await this: ``await timeline.lock()``.
        """
        return self._verb("POST", f"/timelines/{self.uuid}/lock", self._apply_lock)

    def add_participant(self, user: User | str):
        """Add a peer to this timeline (owner or admin only; mutual trust required).

        Accepts a ``User`` or a uuid. Idempotent. The added user is appended to
        ``.participants`` and returned.

        With ``AsyncBaseCradle``, await this: ``await timeline.add_participant(user)``.
        """
        return self._verb(
            "POST",
            f"/timelines/{self.uuid}/participations",
            self._apply_added_participant,
            json={"user_id": _uuid_of(user)},
        )

    def remove_participant(self, user: User | str):
        """Remove a participant from this timeline (owner or admin only). Idempotent.

        With ``AsyncBaseCradle``, await this: ``await timeline.remove_participant(user)``.
        """
        uuid = _uuid_of(user)
        return self._verb(
            "DELETE",
            f"/timelines/{self.uuid}/participations/{uuid}",
            lambda _response: self._apply_removed_participant(uuid),
        )

    # -- live-object state updates (what the API confirmed) --

    def _apply_lock(self, response: dict[str, Any]) -> None:
        self._data["locked"] = response["locked"]

    def _apply_added_participant(self, response: dict[str, Any]) -> User:
        added = User(response, client=self._client)
        participants = self._data.setdefault("participants", [])
        if not any(existing["uuid"] == added.uuid for existing in participants):
            participants.append(response)
        return added

    def _apply_removed_participant(self, uuid: str) -> None:
        participants = self._data.get("participants", [])
        self._data["participants"] = [p for p in participants if p["uuid"] != uuid]


# --- resources -----------------------------------------------------------------------------


def _create_payload(name: str) -> dict[str, Any]:
    return {"timeline": {"name": name}}


def _subject_timeline(response: dict[str, Any], client: Any) -> Timeline:
    """Merge the API's two-key envelope ({"timeline": ..., "items": ...}) into one Timeline."""
    return Timeline({**response["timeline"], "items": response["items"]}, client=client)


class _TimelinesResourceCore:
    """What the sync and async timeline resources share."""

    def __init__(self, client: Any) -> None:
        self._client = client


class TimelinesResource(_TimelinesResourceCore):
    """Your timelines — the ones you own plus the ones you participate in.

    Iterable (auto-paginating, newest first): ``for timeline in bc.timelines``.
    """

    def __iter__(self) -> Iterator[Timeline]:
        return paginate(self._client, "/timelines", envelope_key="timelines", model=Timeline)

    def create(self, *, name: str) -> Timeline:
        """Create a timeline owned by you (subject to your ``max_timelines`` cap)."""
        response = self._client.request("POST", "/timelines", json=_create_payload(name))
        return _subject_timeline(response, self._client)

    def get(self, uuid: str) -> Timeline:
        """Fetch one timeline with its items inline (you must be a viewer)."""
        response = self._client.request("GET", f"/timelines/{uuid}")
        return _subject_timeline(response, self._client)


class AsyncTimelinesResource(_TimelinesResourceCore):
    """Your timelines, async: ``async for timeline in abc.timelines``."""

    def __aiter__(self) -> AsyncIterator[Timeline]:
        return apaginate(self._client, "/timelines", envelope_key="timelines", model=Timeline)

    async def create(self, *, name: str) -> Timeline:
        """Create a timeline owned by you (subject to your ``max_timelines`` cap)."""
        response = await self._client.request("POST", "/timelines", json=_create_payload(name))
        return _subject_timeline(response, self._client)

    async def get(self, uuid: str) -> Timeline:
        """Fetch one timeline with its items inline (you must be a viewer)."""
        response = await self._client.request("GET", f"/timelines/{uuid}")
        return _subject_timeline(response, self._client)
