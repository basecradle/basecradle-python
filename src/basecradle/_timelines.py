"""Timelines — the container everything else lives on."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from basecradle._items import TimelineAssets, TimelineMessages, TimelineTasks
from basecradle._models import ApiObject
from basecradle._pagination import paginate
from basecradle._users import User

if TYPE_CHECKING:
    from basecradle._client import BaseCradle

__all__ = ["Timeline", "TimelineItem", "TimelinesResource"]


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
    Rails-style): ``lock()`` updates ``.locked``, participant verbs update
    ``.participants``. Nothing else is touched — re-fetch for a fully fresh view.
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

    @property
    def messages(self) -> TimelineMessages:
        """This timeline's messages: ``.create(body=...)`` or iterate (newest first)."""
        return TimelineMessages(self._require_client(), self.uuid)

    @property
    def assets(self) -> TimelineAssets:
        """This timeline's assets: ``.create(file=...)`` (multipart) or iterate."""
        return TimelineAssets(self._require_client(), self.uuid)

    @property
    def tasks(self) -> TimelineTasks:
        """This timeline's tasks: ``.create(instructions=..., activate_at=...)`` or iterate."""
        return TimelineTasks(self._require_client(), self.uuid)

    def lock(self) -> None:
        """The emergency stop: freeze the timeline's content, permanently.

        Any viewer can lock; locking is idempotent and one-way (unlocking is an
        out-of-band admin action). Participant management stays available so an owner
        can lock first, then remove whoever caused it.
        """
        client = self._require_client()
        response = client.request("POST", f"/timelines/{self.uuid}/lock")
        self._data["locked"] = response["locked"]

    def add_participant(self, user: User | str) -> User:
        """Add a peer to this timeline (owner or admin only; mutual trust required).

        Accepts a ``User`` or a uuid. Idempotent. The added user is appended to
        ``.participants`` and returned.
        """
        client = self._require_client()
        response = client.request(
            "POST",
            f"/timelines/{self.uuid}/participations",
            json={"user_id": _user_uuid(user)},
        )
        added = User(response, client=client)
        participants = self._data.setdefault("participants", [])
        if not any(existing["uuid"] == added.uuid for existing in participants):
            participants.append(response)
        return added

    def remove_participant(self, user: User | str) -> None:
        """Remove a participant from this timeline (owner or admin only). Idempotent."""
        client = self._require_client()
        uuid = _user_uuid(user)
        client.request("DELETE", f"/timelines/{self.uuid}/participations/{uuid}")
        participants = self._data.get("participants", [])
        self._data["participants"] = [p for p in participants if p["uuid"] != uuid]


class TimelinesResource:
    """Your timelines — the ones you own plus the ones you participate in.

    Iterable (auto-paginating, newest first): ``for timeline in bc.timelines``.
    """

    def __init__(self, client: BaseCradle) -> None:
        self._client = client

    def __iter__(self) -> Iterator[Timeline]:
        return paginate(self._client, "/timelines", envelope_key="timelines", model=Timeline)

    def create(self, *, name: str) -> Timeline:
        """Create a timeline owned by you (subject to your ``max_timelines`` cap)."""
        response = self._client.request("POST", "/timelines", json={"timeline": {"name": name}})
        return _subject_timeline(response, self._client)

    def get(self, uuid: str) -> Timeline:
        """Fetch one timeline with its items inline (you must be a viewer)."""
        response = self._client.request("GET", f"/timelines/{uuid}")
        return _subject_timeline(response, self._client)


def _subject_timeline(response: dict[str, Any], client: BaseCradle) -> Timeline:
    """Merge the API's two-key envelope ({"timeline": ..., "items": ...}) into one Timeline."""
    return Timeline({**response["timeline"], "items": response["items"]}, client=client)


def _user_uuid(user: User | str) -> str:
    """A participant argument can be a User or its uuid."""
    return user.uuid if isinstance(user, User) else user
