"""Contact messages — what people send through the public contact page. **Admin-only.**

A contact message is a name, an email address and a message, stored with everything the
request said about itself (IP address, user agent, headers, whether the honeypot was
filled, how long the form took) and, afterwards, what the risk-assessment vendors said
about it (``data``). Nothing is rejected on a score: the record is the data, and an admin
decides — by setting its ``status`` and writing notes on it.

Every operation answers ``403`` (``NotAnAdminError``) to anyone who is not an admin.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from basecradle._headers import RequestHeaders
from basecradle._models import ApiObject
from basecradle._notes import Note
from basecradle._pagination import apaginate, paginate
from basecradle._resources import Resource
from basecradle._users import User

__all__ = ["AsyncContactMessagesResource", "ContactMessage", "ContactMessagesResource"]


class ContactMessage(ApiObject):
    """One contact-form submission, with its triage state and its notes.

    A top-level record, so it is flat (no ``type``/``content``). ``user`` is the signed-in
    peer who sent it, in nested-actor form, or ``None`` for a visitor without an account.

    ``data`` holds one self-describing slot per risk-assessment vendor — each carries
    ``vendor``, ``api``, ``docs``, ``fetched_at`` and ``attempts``, then exactly one of
    ``answer``, ``skipped`` or ``error``. It is returned as the plain ``dict`` the wire sent:
    the vendors and their payloads are the platform's to change, not this SDK's to model.

    ``headers`` is the request's headers, read through ``RequestHeaders`` like a webhook
    delivery's: lookup folds case, and ``repr()`` prints the names, never the values. A
    header the platform recorded without a value reads ``None``.

    ``notes`` is every note on this message, embedded in full. With ``AsyncBaseCradle``,
    await the verbs: ``await message.set_status("closed")``.
    """

    uuid: str
    name: str
    email_address: str
    body: str
    status: str  # "received" | "closed" | "spam"
    user: User | None  # the sender, if signed in; None for a visitor without an account
    ip_address: str
    user_agent: str
    headers: RequestHeaders[str | None]  # wire-exact pairs; lookup folds case
    honeypot_filled: bool
    fill_seconds: int | None  # how long the form took to fill, when known
    data: dict[str, Any]  # one self-describing slot per vendor — opaque by design
    notes: list[Note]
    created_at: str
    updated_at: str

    def set_status(self, status: str) -> Any:
        """Set the triage verdict: ``"received"``, ``"closed"`` or ``"spam"``.

        It moves in any direction, so an admin can reopen a closed message by setting it
        back to ``"received"``. Live object: the API returns the whole message and this
        object adopts it. A value outside the three raises ``ValidationError``.

        With ``AsyncBaseCradle``, await this: ``await message.set_status("closed")``.
        """
        return self._verb(
            "PATCH",
            f"/contact_messages/{self.uuid}/status",
            self._adopt,
            json={"contact_message": {"status": status}},
        )

    def add_note(self, *, body: str) -> Any:
        """Write a note about this message, and return it. A blank body raises ``ValidationError``.

        A note can never be edited or deleted afterwards, by anyone. The new note is also
        appended to this object's ``notes``.

        With ``AsyncBaseCradle``, await this: ``await message.add_note(body="...")``.
        """
        return self._verb(
            "POST",
            f"/contact_messages/{self.uuid}/notes",
            self._apply_added_note,
            json={"note": {"body": body}},
        )

    def _wrap(self, name: str, value: Any) -> Any:
        """As ``ApiObject``, plus the one field presented richer than its wire type."""
        if name == "headers" and isinstance(value, dict):
            return RequestHeaders(value)
        return super()._wrap(name, value)

    def _adopt(self, response: dict[str, Any]) -> None:
        """Live-object update: the API returned the complete message; adopt it."""
        self._data.clear()
        self._data.update(response["contact_message"])

    def _apply_added_note(self, response: dict[str, Any]) -> Note:
        """Append the confirmed note — the API returns it enveloped, in full."""
        note = response["note"]
        notes = self._data.setdefault("notes", [])
        if not any(existing["uuid"] == note["uuid"] for existing in notes):
            notes.append(note)
        return Note(note, client=self._client)


# --- resources -----------------------------------------------------------------------------


def _contact_message_from(response: dict[str, Any], client: Any) -> ContactMessage:
    return ContactMessage(response["contact_message"], client=client)


class _ContactMessagesResourceCore(Resource):
    def __init__(self, client: Any, filters: dict[str, str] | None = None) -> None:
        self._client = client
        self._filters = filters or {}

    def _repr_fields(self) -> dict[str, Any]:
        return {"path": "/contact_messages", "filters": self._filters}

    def _status_filters(self, status: str | None) -> dict[str, str]:
        filters = dict(self._filters)
        if status is not None:
            filters["status"] = status
        return filters


class ContactMessagesResource(_ContactMessagesResourceCore):
    """Every contact message, newest first — auto-paginating, filterable. Admin-only.

    >>> for message in bc.contact_messages.filter(status="received"):
    ...     print(message.name, message.email_address, message.body)
    """

    def __iter__(self) -> Iterator[ContactMessage]:
        return paginate(
            self._client,
            "/contact_messages",
            envelope_key="contact_messages",
            model=ContactMessage,
            params=self._filters,
        )

    def filter(self, *, status: str | None = None) -> ContactMessagesResource:
        """A new lazy resource narrowed by ``status``: ``received``, ``closed`` or ``spam``."""
        return ContactMessagesResource(self._client, filters=self._status_filters(status))

    def get(self, uuid: str) -> ContactMessage:
        """Fetch one contact message by its uuid."""
        response = self._client.request("GET", f"/contact_messages/{uuid}")
        return _contact_message_from(response, self._client)


class AsyncContactMessagesResource(_ContactMessagesResourceCore):
    """Every contact message, async: ``async for message in abc.contact_messages``."""

    def __aiter__(self) -> AsyncIterator[ContactMessage]:
        return apaginate(
            self._client,
            "/contact_messages",
            envelope_key="contact_messages",
            model=ContactMessage,
            params=self._filters,
        )

    def filter(self, *, status: str | None = None) -> AsyncContactMessagesResource:
        """A new lazy resource narrowed by ``status``. See ``ContactMessagesResource.filter``."""
        return AsyncContactMessagesResource(self._client, filters=self._status_filters(status))

    async def get(self, uuid: str) -> ContactMessage:
        """Fetch one contact message by its uuid. See ``ContactMessagesResource.get``."""
        response = await self._client.request("GET", f"/contact_messages/{uuid}")
        return _contact_message_from(response, self._client)
