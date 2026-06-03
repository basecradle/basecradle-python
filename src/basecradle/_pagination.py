"""The shared cursor-pagination engine.

Every list endpoint in the API paginates the same way: newest first, up to 50 per page,
``next_cursor`` in the response passed back as ``?before=`` for the next (older) page,
``null`` cursor meaning the end. This module is that contract, written once — every list
resource (timelines, messages, assets, tasks, webhooks, sessions, users) iterates
through here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from basecradle._client import BaseCradle
    from basecradle._models import ApiObject

T = TypeVar("T", bound="ApiObject")

__all__ = ["paginate"]


def paginate(
    client: BaseCradle,
    path: str,
    *,
    envelope_key: str,
    model: type[T],
    params: dict[str, Any] | None = None,
) -> Iterator[T]:
    """Yield every item across every page of a cursor-paginated list endpoint.

    Lazy: the first page is fetched when iteration starts, and page N+1 is fetched only
    when iteration crosses the page boundary — cursors never appear in calling code.
    """
    cursor: str | None = None
    while True:
        page_params: dict[str, Any] = {**(params or {})}
        if cursor is not None:
            page_params["before"] = cursor
        page = client.request("GET", path, params=page_params)

        for data in page[envelope_key]:
            yield model(data, client=client)

        cursor = page["next_cursor"]
        if cursor is None:
            return
