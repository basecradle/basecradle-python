"""Timeline items — messages, assets, and tasks. Three resources, one pattern.

Every item shares one envelope shape: ``type``, ``created_at``, ``user`` (nested-actor
form), ``timeline`` (reference form — just a uuid to dereference), and a type-specific
``content``. Each has a nested creator (``timeline.messages.create(...)``) and a
top-level, cross-timeline list + get (``bc.messages``, ``bc.messages.get(uuid)``).

Filterable lists use ``.filter(...)`` — the one idiom, everywhere: it returns a new lazy
iterable resource; filters compose; values may be model objects or uuid strings.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, TypeVar

from basecradle._models import ApiObject
from basecradle._pagination import paginate
from basecradle._users import User

if TYPE_CHECKING:
    from basecradle._client import BaseCradle

__all__ = [
    "Asset",
    "AssetContent",
    "AssetFile",
    "AssetsResource",
    "Item",
    "ItemsResource",
    "Message",
    "MessageContent",
    "MessagesResource",
    "Task",
    "TaskContent",
    "TasksResource",
    "TimelineAssets",
    "TimelineMessages",
    "TimelineTasks",
]


# --- models -------------------------------------------------------------------------------


class Item(ApiObject):
    """The envelope shape every timeline item shares.

    ``timeline`` is in reference form (just a uuid) — dereference it with
    ``bc.timelines.get(item.timeline.uuid)`` when you need the detail.
    """

    type: str  # "message" | "asset" | "task"
    created_at: str
    user: User
    timeline: ApiObject


class MessageContent(ApiObject):
    """A message's content: its uuid and body."""

    uuid: str
    body: str


class Message(Item):
    """A text post on a timeline."""

    content: MessageContent


class AssetFile(ApiObject):
    """An asset's attached file: metadata plus a dereferenceable download URL."""

    filename: str
    byte_size: int
    content_type: str
    checksum: str  # base64 MD5 of the blob
    url: str


class AssetContent(ApiObject):
    """An asset's content: description and the attached file."""

    uuid: str
    description: str
    file: AssetFile


class Asset(Item):
    """A file (with optional description) posted to a timeline."""

    content: AssetContent


class TaskContent(ApiObject):
    """A task's content: instructions, schedule, and status."""

    uuid: str
    instructions: str
    activate_at: str
    status: str  # "pending" | "activated" | "blocked_timeline_locked"


class Task(Item):
    """An instruction with a scheduled activation time."""

    content: TaskContent


# --- top-level resources: iterate / filter / get -----------------------------------------

ItemT = TypeVar("ItemT", bound=Item)


class ItemsResource:
    """The shared cross-timeline list + get pattern. Subclasses bind path, envelope, model."""

    _path: str
    _plural: str
    _singular: str
    _model: type[ApiObject]

    def __init__(self, client: BaseCradle, filters: dict[str, str] | None = None) -> None:
        self._client = client
        self._filters = filters or {}

    def __iter__(self) -> Iterator[Any]:
        return paginate(
            self._client,
            self._path,
            envelope_key=self._plural,
            model=self._model,
            params=self._filters,
        )

    def filter(self, *, timeline: Any | None = None) -> ItemsResource:
        """A new lazy resource narrowed to one timeline (a ``Timeline`` or a uuid)."""
        return type(self)(self._client, filters=self._merge_filters(timeline=timeline))

    def get(self, uuid: str) -> Any:
        """Fetch one item by its own uuid (you must be a viewer of its timeline)."""
        response = self._client.request("GET", f"{self._path}/{uuid}")
        return self._model(response[self._singular], client=self._client)

    def _merge_filters(self, **values: Any) -> dict[str, str]:
        merged = dict(self._filters)
        for key, value in values.items():
            if value is not None:
                merged[key] = _uuid_of(value)
        return merged


class MessagesResource(ItemsResource):
    """Messages from every timeline you can view, newest first."""

    _path = "/messages"
    _plural = "messages"
    _singular = "message"
    _model = Message


class AssetsResource(ItemsResource):
    """Assets from every timeline you can view, newest first."""

    _path = "/assets"
    _plural = "assets"
    _singular = "asset"
    _model = Asset


class TasksResource(ItemsResource):
    """Tasks from every timeline you can view, newest first."""

    _path = "/tasks"
    _plural = "tasks"
    _singular = "task"
    _model = Task

    def filter(self, *, timeline: Any | None = None, status: str | None = None) -> TasksResource:
        """A new lazy resource narrowed by timeline and/or status.

        ``status`` is one of ``pending``, ``activated``, ``blocked_timeline_locked``.
        """
        filters = self._merge_filters(timeline=timeline)
        if status is not None:
            filters["status"] = status
        return TasksResource(self._client, filters=filters)


# --- nested creators: timeline.messages / .assets / .tasks -------------------------------


class TimelineMessages:
    """One timeline's messages: create here, or iterate (newest first)."""

    def __init__(self, client: BaseCradle, timeline_uuid: str) -> None:
        self._client = client
        self._timeline_uuid = timeline_uuid

    def create(self, *, body: str) -> Message:
        """Post a message to this timeline (you must be a viewer; timeline must be unlocked)."""
        response = self._client.request(
            "POST",
            f"/timelines/{self._timeline_uuid}/messages",
            json={"message": {"body": body}},
        )
        return Message(response["message"], client=self._client)

    def __iter__(self) -> Iterator[Message]:
        return iter(MessagesResource(self._client).filter(timeline=self._timeline_uuid))


class TimelineAssets:
    """One timeline's assets: upload here, or iterate (newest first)."""

    def __init__(self, client: BaseCradle, timeline_uuid: str) -> None:
        self._client = client
        self._timeline_uuid = timeline_uuid

    def create(self, *, file: str | Path | IO[bytes], description: str | None = None) -> Asset:
        """Upload a file to this timeline (multipart). ``file`` is a path or a binary file object."""
        filename, fileobj = _open_upload(file)
        try:
            data = {"asset[description]": description} if description is not None else {}
            response = self._client.request(
                "POST",
                f"/timelines/{self._timeline_uuid}/assets",
                files={"asset[file]": (filename, fileobj)},
                data=data,
            )
        finally:
            if fileobj is not file:  # we opened it from a path, so we close it
                fileobj.close()
        return Asset(response["asset"], client=self._client)

    def __iter__(self) -> Iterator[Asset]:
        return iter(AssetsResource(self._client).filter(timeline=self._timeline_uuid))


class TimelineTasks:
    """One timeline's tasks: create here, or iterate (newest first)."""

    def __init__(self, client: BaseCradle, timeline_uuid: str) -> None:
        self._client = client
        self._timeline_uuid = timeline_uuid

    def create(self, *, instructions: str, activate_at: datetime | str) -> Task:
        """Schedule a task on this timeline.

        ``activate_at`` accepts a ``datetime`` (serialized to ISO 8601 — make it
        timezone-aware to be unambiguous; a naive value is interpreted in your account's
        time zone) or an ISO 8601 string.
        """
        if isinstance(activate_at, datetime):
            activate_at = activate_at.isoformat()
        response = self._client.request(
            "POST",
            f"/timelines/{self._timeline_uuid}/tasks",
            json={"task": {"instructions": instructions, "activate_at": activate_at}},
        )
        return Task(response["task"], client=self._client)

    def __iter__(self) -> Iterator[Task]:
        return iter(TasksResource(self._client).filter(timeline=self._timeline_uuid))


# --- helpers ------------------------------------------------------------------------------


def _uuid_of(value: Any) -> str:
    """A filter value can be a model object or a uuid string.

    A model's identity is its top-level ``uuid`` (timelines, users) or, failing that, its
    ``content.uuid`` (items, webhook endpoints) — mirroring how the API addresses them.
    """
    if not isinstance(value, ApiObject):
        return value
    if "uuid" in value._data:
        return value._data["uuid"]
    return value._data["content"]["uuid"]


def _open_upload(file: str | Path | IO[bytes]) -> tuple[str, IO[bytes]]:
    """Resolve an upload argument into (filename, binary file object)."""
    if isinstance(file, (str, Path)):
        path = Path(file)
        return path.name, path.open("rb")
    name = getattr(file, "name", None)
    filename = os.path.basename(name) if isinstance(name, str) else "file"
    return filename, file
