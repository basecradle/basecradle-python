"""The shared cursor-pagination engine — sync and async on one core.

Every list endpoint in the API paginates the same way: newest first, up to 50 per page,
``next_cursor`` in the response passed back as ``?before=`` for the next (older) page,
``null`` cursor meaning the end, and ``total_count`` beside it: how many records the whole
list holds under the filters passed, every page counted. The page logic lives here, once,
in ``_ListIteratorCore``; ``ListIterator`` and ``AsyncListIterator`` are the thin sync and
async loops over it, and ``paginate`` / ``apaginate`` build them.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Iterator
from functools import cached_property
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from basecradle._resources import Resource

if TYPE_CHECKING:
    from basecradle._models import ApiObject

T = TypeVar("T", bound="ApiObject")

__all__ = ["AsyncListIterator", "ListIterator", "apaginate", "paginate"]


class _ListIteratorCore(Resource, Generic[T]):
    """What the sync and async iterators share: the page state and how a page lands in it.

    Nothing is fetched at construction. A page's records go into a buffer that iteration
    drains; the next page is fetched only when the buffer is empty and the last page's
    cursor said there is more.
    """

    def __init__(
        self,
        client: Any,
        path: str,
        *,
        envelope_key: str,
        model: type[T],
        params: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._path = path
        self._envelope_key = envelope_key
        self._model = model
        self._params = params or {}
        self._buffer: deque[T] = deque()
        self._cursor: str | None = None
        self._page: dict[str, Any] | None = None  # the most recently fetched page

    def _repr_fields(self) -> dict[str, Any]:
        """The list this iterates and its filters — never the client."""
        return {"path": self._path, "filters": self._params}

    def _next_page_params(self) -> dict[str, Any]:
        """The query parameters for the next page: the caller's filters plus the cursor."""
        page_params: dict[str, Any] = {**self._params}
        if self._cursor is not None:
            page_params["before"] = self._cursor
        return page_params

    def _absorb(self, page: dict[str, Any]) -> None:
        """One page's response → its typed records buffered, its cursor and total kept.

        Everything is read before anything is kept, so a page that fails to read changes
        nothing: iterating again re-fetches it, and no record is buffered twice.

        A page without ``next_cursor`` is the last one. Every list sends it, and the spec
        requires it on every list but ``GET /users``, which declares it without requiring
        it; a list with no cursor has nothing more to page to.
        """
        records = [self._model(data, client=self._client) for data in page[self._envelope_key]]
        cursor = page.get("next_cursor")
        self._buffer.extend(records)
        self._cursor = cursor
        self._page = page

    @property
    def _fetched_a_page(self) -> bool:
        return self._page is not None

    @property
    def _exhausted(self) -> bool:
        """Every page is fetched: the last one's cursor was ``null``."""
        return self._fetched_a_page and self._cursor is None

    def _read_total_count(self) -> int:
        """``total_count`` as the most recently fetched page reported it.

        A page that did not carry it raises ``AttributeError``, the way a model field the
        API did not return does: the SDK never invents a count the API withheld. Iteration
        itself never needs it, so a page without it still iterates.
        """
        if self._page is None or "total_count" not in self._page:
            raise AttributeError(
                f"The {self._path} list page did not carry a total_count, so there is none "
                f"to read. Iterating it still works."
            )
        total_count: int = self._page["total_count"]
        return total_count


class ListIterator(_ListIteratorCore[T], Iterator[T]):
    """Every record of a list, across every page — and the list's ``total_count``.

    Lazy: the first page is fetched when iteration starts (or ``total_count`` is read), and
    page N+1 only when iteration crosses the page boundary — cursors never appear in
    calling code. What ``iter(bc.timelines)`` and every other list resource return.

    >>> timelines = iter(bc.timelines)
    >>> timelines.total_count  # fetches page 1 if it is not here yet; consumes nothing
    128
    >>> for timeline in timelines:  # starts from the first record, page 1 not re-fetched
    ...     print(timeline.name)
    """

    def __iter__(self) -> ListIterator[T]:
        return self

    def __next__(self) -> T:
        while not self._buffer:
            if self._exhausted:
                raise StopIteration
            self._fetch_next_page()
        return self._buffer.popleft()

    @property
    def total_count(self) -> int:
        """How many records the whole list holds under its filters, every page counted.

        Reading it fetches the first page if iteration has not, and consumes no record:
        iterating afterwards still starts at the first one, without fetching page 1 again.
        It is the count the most recently fetched page reported, so it does not shrink as
        you page; it moves only if records are added or removed while you do.
        """
        if not self._fetched_a_page:
            self._fetch_next_page()
        return self._read_total_count()

    def _fetch_next_page(self) -> None:
        self._absorb(self._client.request("GET", self._path, params=self._next_page_params()))


class AsyncListIterator(_ListIteratorCore[T], AsyncIterator[T]):
    """``ListIterator`` for ``AsyncBaseCradle``: ``async for``, and ``await .total_count``.

    Same laziness, same cursor invisibility. ``total_count`` is awaited, like ``bc.me`` is
    on the async client, because reading it may have to fetch the first page.

    >>> timelines = aiter(abc.timelines)
    >>> await timelines.total_count
    128
    >>> async for timeline in timelines:
    ...     print(timeline.name)
    """

    @cached_property
    def _fetching(self) -> asyncio.Lock:
        """Held across each fetch, so two tasks sharing this iterator (``asyncio.gather``
        of ``total_count`` and ``anext``) cannot both fetch page 1 and buffer it twice.

        Made on first use; nothing awaits between the check and the store, so two tasks
        cannot each make one.
        """
        return asyncio.Lock()

    def __aiter__(self) -> AsyncListIterator[T]:
        return self

    async def __anext__(self) -> T:
        async with self._fetching:
            while not self._buffer:
                if self._exhausted:
                    raise StopAsyncIteration
                await self._fetch_next_page()
            return self._buffer.popleft()

    @property
    def total_count(self) -> Awaitable[int]:
        """``ListIterator.total_count``, awaited: ``await records.total_count``."""
        return self._total_count()

    async def _total_count(self) -> int:
        async with self._fetching:
            if not self._fetched_a_page:
                await self._fetch_next_page()
        return self._read_total_count()

    async def _fetch_next_page(self) -> None:
        self._absorb(await self._client.request("GET", self._path, params=self._next_page_params()))


def paginate(
    client: Any,
    path: str,
    *,
    envelope_key: str,
    model: type[T],
    params: dict[str, Any] | None = None,
) -> ListIterator[T]:
    """Every record across every page of a cursor-paginated list endpoint, lazily."""
    return ListIterator(client, path, envelope_key=envelope_key, model=model, params=params)


def apaginate(
    client: Any,
    path: str,
    *,
    envelope_key: str,
    model: type[T],
    params: dict[str, Any] | None = None,
) -> AsyncListIterator[T]:
    """``paginate`` for AsyncBaseCradle — same laziness, same cursor invisibility."""
    return AsyncListIterator(client, path, envelope_key=envelope_key, model=model, params=params)
