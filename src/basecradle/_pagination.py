"""The shared cursor-pagination engine — sync and async on one core.

Every list endpoint in the API paginates the same way: newest first, up to 50 per page,
``next_cursor`` in the response passed back as ``?before=`` for the next (older) page,
``null`` cursor meaning the end. The cursor logic lives here, once; ``paginate`` and
``apaginate`` are the thin sync/async loops over it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from basecradle._models import ApiObject

T = TypeVar("T", bound="ApiObject")

__all__ = ["apaginate", "paginate"]


def _page_params(params: dict[str, Any] | None, cursor: str | None) -> dict[str, Any]:
    """The query parameters for one page: the caller's filters plus the cursor."""
    page_params: dict[str, Any] = {**(params or {})}
    if cursor is not None:
        page_params["before"] = cursor
    return page_params


def _page_items(
    page: dict[str, Any], envelope_key: str, model: type[T], client: Any
) -> tuple[list[T], str | None]:
    """One page's response → (typed items, the next cursor)."""
    items = [model(data, client=client) for data in page[envelope_key]]
    return items, page["next_cursor"]


def paginate(
    client: Any,
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
        page = client.request("GET", path, params=_page_params(params, cursor))
        items, cursor = _page_items(page, envelope_key, model, client)
        yield from items
        if cursor is None:
            return


async def apaginate(
    client: Any,
    path: str,
    *,
    envelope_key: str,
    model: type[T],
    params: dict[str, Any] | None = None,
) -> AsyncIterator[T]:
    """``paginate`` for AsyncBaseCradle — same laziness, same cursor invisibility."""
    cursor: str | None = None
    while True:
        page = await client.request("GET", path, params=_page_params(params, cursor))
        items, cursor = _page_items(page, envelope_key, model, client)
        for item in items:
            yield item
        if cursor is None:
            return
