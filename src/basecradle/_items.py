"""Timeline items — messages, assets, and tasks. Three resources, one pattern, two clients.

Every item shares one envelope shape: ``type``, ``created_at``, ``updated_at``, ``user``
(nested-actor form), ``timeline`` (reference form — just a uuid to dereference), and a
type-specific ``content``. Each has a nested creator (``timeline.messages.create(...)``)
and a top-level, cross-timeline list + get (``bc.messages``, ``bc.messages.get(uuid)``).

Filterable lists use ``.filter(...)`` — the one idiom, everywhere: it returns a new lazy
iterable resource; filters compose; values may be model objects or uuid strings.

Sync and async resources share everything except the I/O: the bindings (paths, envelopes,
models), the filter logic, the payload builders, and the response handlers are written once.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Generic, TypeVar, cast

from basecradle._models import ApiObject
from basecradle._pagination import apaginate, paginate
from basecradle._resources import Resource
from basecradle._users import User

__all__ = [
    "Asset",
    "AssetContent",
    "AssetFile",
    "AssetsResource",
    "AsyncAssetsResource",
    "AsyncItemsResource",
    "AsyncMessagesResource",
    "AsyncTasksResource",
    "AsyncTimelineAssets",
    "AsyncTimelineMessages",
    "AsyncTimelineTasks",
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


# --- models (shared by both clients) ------------------------------------------------------


class Item(ApiObject):
    """The envelope shape every timeline item shares.

    ``timeline`` is in reference form (just a uuid) — dereference it with
    ``bc.timelines.get(item.timeline.uuid)`` when you need the detail. ``created_at`` is
    when the record was made; ``updated_at`` moves when it changes (a task's status, say),
    so you can tell a refreshed record from a stale one without diffing it.
    """

    type: str  # "message" | "asset" | "task"
    created_at: str
    updated_at: str
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
    status: str  # "pending" | "activated" | "blocked_timeline_locked" | "cancelled"


class Task(Item):
    """An instruction with a scheduled activation time — and the verb to withdraw it.

    With ``AsyncBaseCradle``, verbs return coroutines — await them: ``await task.cancel()``.
    """

    content: TaskContent

    def cancel(self) -> Any:
        """Withdraw this **pending** task before it activates.

        The task's alarm never fires and the slot it held under your
        ``max_pending_tasks`` cap is freed immediately, so the intended pattern is a
        rolling **dead man's switch**: schedule a task, then cancel-and-reschedule it
        each time you check in — if you ever stop, the last task activates. Cancelling
        updates this object's ``content.status`` to ``"cancelled"`` (a terminal state)
        and returns it.

        Author-or-admin only: a non-author gets ``403`` (``NotTaskAuthorError``). Only a
        *pending* task can be cancelled — one that has already activated, blocked, or been
        cancelled gets ``409`` (``TaskNotPendingError``). A **locked** timeline does not
        block cancellation: withdrawing a task is cleanup, not content creation.

        With ``AsyncBaseCradle``, await this: ``await task.cancel()``.
        """
        return self._verb("POST", f"/tasks/{self.content.uuid}/cancellation", self._apply_cancel)

    def _apply_cancel(self, response: dict[str, Any]) -> Task:
        self._data["content"]["status"] = response["task"]["content"]["status"]
        return self


# --- the shared resource core -------------------------------------------------------------


#: The record type a resource yields. Parameterizing the core on it makes the *element
#: type* of `__iter__` / `__aiter__` / `get` a checked claim rather than `Any`: wire
#: `AsyncTimelineMessages.__aiter__` to the assets resource and it is now a type error,
#: where before it type-checked clean (#237).
#:
#: It says nothing about the envelope keys beside it -- `_plural` and `_singular` are
#: plain strings, so a binding that pairs the right model with the wrong `_singular`
#: still fails at runtime (a `KeyError` from `_wrap_subject`) and not here. Tying those
#: to the binding too is the next step, not this one.
_M = TypeVar("_M", bound=ApiObject)

#: For `filter`, which returns *the same* resource narrowed. `typing.Self` would say this
#: directly but is 3.11+, and this SDK supports 3.10 without adding `typing_extensions`
#: (httpx is the only runtime dependency) -- so the pre-`Self` idiom: bind the TypeVar to
#: `self`, and every subclass gets its own type back.
#:
#: One TypeVar per declaring class, deliberately, rather than one shared `_ItemsResourceCore`
#: bound for all three `filter` overrides: a shared bound would make every resource an
#: acceptable `self` for every `filter`, so `_TasksBinding.filter(bc.messages, status=...)`
#: would type-check and post a `status` filter at `/messages`, which the API rejects.
_R = TypeVar("_R", bound="_ItemsResourceCore[Any]")
_RTasks = TypeVar("_RTasks", bound="_TasksBinding")


class _ItemsResourceCore(Resource, Generic[_M]):
    """What sync and async resources share: bindings, construction, filter logic."""

    _path: str
    _plural: str
    _singular: str
    _model: type[_M]

    def __init__(self, client: Any, filters: dict[str, str] | None = None) -> None:
        self._client = client
        self._filters = filters or {}

    def _repr_fields(self) -> dict[str, Any]:
        """The endpoint this resource lists and the filters narrowing it — its whole state.

        Filters are shown even when empty: ``filters={}`` is the difference between a
        resource that will list everything you can see and one a ``filter(...)`` already
        narrowed, which is the first thing you want from a repr of a lazy list.

        ``_path`` is read defensively because ``ItemsResource`` and ``AsyncItemsResource``
        are exported unbound — there ``_path`` is a bare annotation and only the
        per-resource bindings supply a value — and a repr that raises takes the whole
        ``logging`` record down with it.
        """
        path = getattr(self, "_path", None)
        if path is None:
            return {"filters": self._filters}
        return {"path": path, "filters": self._filters}

    def filter(self: _R, *, timeline: Any | None = None) -> _R:
        """A new lazy resource narrowed to one timeline (a ``Timeline`` or a uuid)."""
        return type(self)(self._client, filters=self._merge_filters(timeline=timeline))

    def _merge_filters(self, **values: Any) -> dict[str, str]:
        merged = dict(self._filters)
        for key, value in values.items():
            if value is not None:
                merged[key] = _uuid_of(value)
        return merged

    def _wrap_subject(self, response: dict[str, Any]) -> _M:
        return self._model(response[self._singular], client=self._client)


class ItemsResource(_ItemsResourceCore[_M]):
    """The sync cross-timeline list + get pattern."""

    def __iter__(self) -> Iterator[_M]:
        return paginate(
            self._client,
            self._path,
            envelope_key=self._plural,
            model=self._model,
            params=self._filters,
        )

    def get(self, uuid: str) -> _M:
        """Fetch one item by its own uuid (you must be a viewer of its timeline)."""
        return self._wrap_subject(self._client.request("GET", f"{self._path}/{uuid}"))


class AsyncItemsResource(_ItemsResourceCore[_M]):
    """The async cross-timeline list + get pattern: ``async for`` / ``await .get()``."""

    def __aiter__(self) -> AsyncIterator[_M]:
        return apaginate(
            self._client,
            self._path,
            envelope_key=self._plural,
            model=self._model,
            params=self._filters,
        )

    async def get(self, uuid: str) -> _M:
        """Fetch one item by its own uuid (you must be a viewer of its timeline)."""
        return self._wrap_subject(await self._client.request("GET", f"{self._path}/{uuid}"))


# --- per-resource bindings (declared once, used by both sync and async) --------------------


class _MessagesBinding(_ItemsResourceCore[Message]):
    """Messages from every timeline you can view, newest first."""

    _path = "/messages"
    _plural = "messages"
    _singular = "message"
    _model = Message


class _AssetsBinding(_ItemsResourceCore[Asset]):
    """Assets from every timeline you can view, newest first."""

    _path = "/assets"
    _plural = "assets"
    _singular = "asset"
    _model = Asset


class _TasksBinding(_ItemsResourceCore[Task]):
    """Tasks from every timeline you can view, newest first."""

    _path = "/tasks"
    _plural = "tasks"
    _singular = "task"
    _model = Task

    def filter(self: _RTasks, *, timeline: Any | None = None, status: str | None = None) -> _RTasks:
        """A new lazy resource narrowed by timeline and/or status.

        ``status`` is one of ``pending``, ``activated``, ``blocked_timeline_locked``,
        ``cancelled``.
        """
        filters = self._merge_filters(timeline=timeline)
        if status is not None:
            filters["status"] = status
        return type(self)(self._client, filters=filters)


class MessagesResource(_MessagesBinding, ItemsResource[Message]): ...


class AsyncMessagesResource(_MessagesBinding, AsyncItemsResource[Message]): ...


class AssetsResource(_AssetsBinding, ItemsResource[Asset]): ...


class AsyncAssetsResource(_AssetsBinding, AsyncItemsResource[Asset]): ...


class TasksResource(_TasksBinding, ItemsResource[Task]): ...


class AsyncTasksResource(_TasksBinding, AsyncItemsResource[Task]): ...


# --- nested creators: timeline.messages / .assets / .tasks --------------------------------
#
# The payload builders and response handlers are shared; the sync and async creator classes
# are the thin I/O layers over them.


class _NestedCreatorCore(Resource):
    def __init__(self, client: Any, timeline_uuid: str) -> None:
        self._client = client
        self._timeline_uuid = timeline_uuid

    def _repr_fields(self) -> dict[str, Any]:
        return {"timeline": self._timeline_uuid}


def _idempotency_headers(idempotency_key: str | None) -> dict[str, str] | None:
    """The per-request header for a keyed create, or ``None`` when no key was given.

    Shared by every create method (here and in ``_webhooks``) so the header name lives in
    exactly one place. The platform treats the value opaquely (a UUID is recommended); a
    replay of the same key returns the original record rather than creating a duplicate.
    """
    if idempotency_key is None:
        return None
    return {"Idempotency-Key": idempotency_key}


def _message_request(timeline_uuid: str, body: str) -> tuple[str, str, dict[str, Any]]:
    return "POST", f"/timelines/{timeline_uuid}/messages", {"message": {"body": body}}


def _message_from(response: dict[str, Any], client: Any) -> Message:
    return Message(response["message"], client=client)


def _task_request(
    timeline_uuid: str, instructions: str, activate_at: datetime | str
) -> tuple[str, str, dict[str, Any]]:
    if isinstance(activate_at, datetime):
        activate_at = activate_at.isoformat()
    payload = {"task": {"instructions": instructions, "activate_at": activate_at}}
    return "POST", f"/timelines/{timeline_uuid}/tasks", payload


def _task_from(response: dict[str, Any], client: Any) -> Task:
    return Task(response["task"], client=client)


def _asset_upload(
    timeline_uuid: str, file: str | Path | IO[bytes], description: str | None
) -> tuple[str, dict[str, Any], dict[str, Any], IO[bytes], bool]:
    """The multipart upload, prepared: (path, files, data, fileobj, we_opened_it)."""
    filename, fileobj, opened = _open_upload(file)
    files = {"asset[file]": (filename, fileobj)}
    data = {"asset[description]": description} if description is not None else {}
    return f"/timelines/{timeline_uuid}/assets", files, data, fileobj, opened


def _asset_from(response: dict[str, Any], client: Any) -> Asset:
    return Asset(response["asset"], client=client)


class TimelineMessages(_NestedCreatorCore):
    """One timeline's messages: create here, or iterate (newest first)."""

    def create(self, *, body: str, idempotency_key: str | None = None) -> Message:
        """Post a message to this timeline (you must be a viewer; timeline must be unlocked).

        Pass ``idempotency_key`` (a UUID is ideal) to make the create safe to retry: a replay
        of the same key returns the original message, never a duplicate. See the client's
        ``max_retries`` for opt-in automatic retry of keyed creates.
        """
        method, path, payload = _message_request(self._timeline_uuid, body)
        response = self._client.request(
            method, path, json=payload, headers=_idempotency_headers(idempotency_key)
        )
        return _message_from(response, self._client)

    def __iter__(self) -> Iterator[Message]:
        return iter(MessagesResource(self._client).filter(timeline=self._timeline_uuid))


class AsyncTimelineMessages(_NestedCreatorCore):
    """One timeline's messages, async: ``await .create(...)`` or ``async for``."""

    async def create(self, *, body: str, idempotency_key: str | None = None) -> Message:
        """Post a message to this timeline. See ``TimelineMessages.create`` for semantics."""
        method, path, payload = _message_request(self._timeline_uuid, body)
        response = await self._client.request(
            method, path, json=payload, headers=_idempotency_headers(idempotency_key)
        )
        return _message_from(response, self._client)

    def __aiter__(self) -> AsyncIterator[Message]:
        return AsyncMessagesResource(self._client).filter(timeline=self._timeline_uuid).__aiter__()


class TimelineAssets(_NestedCreatorCore):
    """One timeline's assets: upload here, or iterate (newest first)."""

    def create(
        self,
        *,
        file: str | Path | IO[bytes],
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> Asset:
        """Upload a file to this timeline (multipart). ``file`` is a path or a binary file object.

        Pass ``idempotency_key`` (a UUID is ideal) to make the upload safe to retry: a replay
        of the same key returns the original asset, never a duplicate. See the client's
        ``max_retries`` for opt-in automatic retry of keyed creates.
        """
        path, files, data, fileobj, opened = _asset_upload(self._timeline_uuid, file, description)
        try:
            response = self._client.request(
                "POST", path, files=files, data=data, headers=_idempotency_headers(idempotency_key)
            )
        finally:
            if opened:
                fileobj.close()
        return _asset_from(response, self._client)

    def __iter__(self) -> Iterator[Asset]:
        return iter(AssetsResource(self._client).filter(timeline=self._timeline_uuid))


class AsyncTimelineAssets(_NestedCreatorCore):
    """One timeline's assets, async: ``await .create(...)`` or ``async for``."""

    async def create(
        self,
        *,
        file: str | Path | IO[bytes],
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> Asset:
        """Upload a file to this timeline (multipart). See ``TimelineAssets.create``."""
        path, files, data, fileobj, opened = _asset_upload(self._timeline_uuid, file, description)
        try:
            response = await self._client.request(
                "POST", path, files=files, data=data, headers=_idempotency_headers(idempotency_key)
            )
        finally:
            if opened:
                fileobj.close()
        return _asset_from(response, self._client)

    def __aiter__(self) -> AsyncIterator[Asset]:
        return AsyncAssetsResource(self._client).filter(timeline=self._timeline_uuid).__aiter__()


class TimelineTasks(_NestedCreatorCore):
    """One timeline's tasks: create here, or iterate (newest first)."""

    def create(
        self,
        *,
        instructions: str,
        activate_at: datetime | str,
        idempotency_key: str | None = None,
    ) -> Task:
        """Schedule a task on this timeline.

        ``activate_at`` accepts a ``datetime`` (serialized to ISO 8601 — make it
        timezone-aware to be unambiguous; a naive value is interpreted in your account's
        time zone) or an ISO 8601 string.

        Pass ``idempotency_key`` (a UUID is ideal) to make the create safe to retry: a replay
        of the same key returns the original task — no duplicate, and no second activation.
        See the client's ``max_retries`` for opt-in automatic retry of keyed creates.
        """
        method, path, payload = _task_request(self._timeline_uuid, instructions, activate_at)
        response = self._client.request(
            method, path, json=payload, headers=_idempotency_headers(idempotency_key)
        )
        return _task_from(response, self._client)

    def __iter__(self) -> Iterator[Task]:
        return iter(TasksResource(self._client).filter(timeline=self._timeline_uuid))


class AsyncTimelineTasks(_NestedCreatorCore):
    """One timeline's tasks, async: ``await .create(...)`` or ``async for``."""

    async def create(
        self,
        *,
        instructions: str,
        activate_at: datetime | str,
        idempotency_key: str | None = None,
    ) -> Task:
        """Schedule a task on this timeline. See ``TimelineTasks.create`` for semantics."""
        method, path, payload = _task_request(self._timeline_uuid, instructions, activate_at)
        response = await self._client.request(
            method, path, json=payload, headers=_idempotency_headers(idempotency_key)
        )
        return _task_from(response, self._client)

    def __aiter__(self) -> AsyncIterator[Task]:
        return AsyncTasksResource(self._client).filter(timeline=self._timeline_uuid).__aiter__()


# --- helpers ------------------------------------------------------------------------------


def _uuid_of(value: Any) -> str:
    """A filter value can be a model object or a uuid string.

    A model's identity is its top-level ``uuid`` (timelines, users) or, failing that, its
    ``content.uuid`` (items, webhook endpoints) — mirroring how the API addresses them.
    """
    if not isinstance(value, ApiObject):
        return cast(str, value)
    if "uuid" in value._data:
        return cast(str, value._data["uuid"])
    return cast(str, value._data["content"]["uuid"])


def _open_upload(file: str | Path | IO[bytes]) -> tuple[str, IO[bytes], bool]:
    """Resolve an upload argument into (filename, binary file object, whether we opened it)."""
    if isinstance(file, (str, Path)):
        path = Path(file)
        return path.name, path.open("rb"), True
    name = getattr(file, "name", None)
    filename = os.path.basename(name) if isinstance(name, str) else "file"
    return filename, file, False
