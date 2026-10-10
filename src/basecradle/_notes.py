"""Notes — an admin's remarks about a record. **Admin-only.**

A note says who wrote it, what it is about, what it says, and when. It is written from its
subject's own endpoint (``contact_message.add_note(...)``) and **never changes**: there is
no update and no delete, for anyone. Today a note's subject is always a contact message;
``notable`` carries a ``type`` beside its ``uuid`` because any record may take notes later.

Every operation answers ``403`` (``NotAnAdminError``) to anyone who is not an admin.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from basecradle._models import ApiObject
from basecradle._pagination import apaginate, paginate
from basecradle._resources import Resource
from basecradle._users import User

__all__ = ["AsyncNotesResource", "Note", "NotesResource"]


class Note(ApiObject):
    """An admin's remark about a record — immutable once written.

    A top-level record, so it is flat (no ``type``/``content``), and it is one shape
    everywhere it appears: ``bc.notes``, ``bc.notes.get(...)``, the note ``add_note()``
    returns, and each entry of a contact message's ``notes``.
    """

    uuid: str
    body: str
    user: User  # the author, in nested-actor form
    notable: ApiObject  # the subject: ``type`` ("contact_message") and ``uuid``
    created_at: str
    updated_at: str


def _note_from(response: dict[str, Any], client: Any) -> Note:
    return Note(response["note"], client=client)


class _NotesResourceCore(Resource):
    def __init__(self, client: Any) -> None:
        self._client = client


class NotesResource(_NotesResourceCore):
    """Every note, across every subject, newest first — auto-paginating. Admin-only.

    >>> for note in bc.notes:
    ...     print(note.user.handle, note.notable.type, note.body)
    """

    def __iter__(self) -> Iterator[Note]:
        return paginate(self._client, "/notes", envelope_key="notes", model=Note)

    def get(self, uuid: str) -> Note:
        """Fetch one note by its uuid."""
        return _note_from(self._client.request("GET", f"/notes/{uuid}"), self._client)


class AsyncNotesResource(_NotesResourceCore):
    """Every note, async: ``async for note in abc.notes``. Admin-only."""

    def __aiter__(self) -> AsyncIterator[Note]:
        return apaginate(self._client, "/notes", envelope_key="notes", model=Note)

    async def get(self, uuid: str) -> Note:
        """Fetch one note by its uuid. See ``NotesResource.get``."""
        return _note_from(await self._client.request("GET", f"/notes/{uuid}"), self._client)
