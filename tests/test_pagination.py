"""The shared pagination engine — generic, lazy, cursor-invisible, and counted.

These tests use a throwaway model and envelope key on purpose: the engine must be
genuinely generic (acceptance criterion: the next list resource reuses it unmodified).
"""

import asyncio
import pickle

import httpx
import pytest

from basecradle import ApiObject, AsyncListIterator, BaseCradleError, ListIterator
from basecradle._pagination import apaginate, paginate

# A deliberately non-Timeline resource, to pin genericity.
WIDGETS_PAGE_1 = {
    "widgets": [{"uuid": "019e7750-66ee-7001-8000-000000000001", "size": "large"}],
    "next_cursor": "019e7750-66ee-7001-8000-000000000001",
    "total_count": 3,
}
WIDGETS_PAGE_2 = {
    "widgets": [{"uuid": "019e7750-66ee-7002-8000-000000000002", "size": "small"}],
    "next_cursor": "019e7750-66ee-7002-8000-000000000002",
    "total_count": 3,
}
WIDGETS_PAGE_3 = {
    "widgets": [{"uuid": "019e7750-66ee-7003-8000-000000000003", "size": "medium"}],
    "next_cursor": None,
    "total_count": 3,
}


class Widget(ApiObject):
    uuid: str
    size: str


class TestPagination:
    def test_iterates_every_page_transparently(self, bc, api):
        api.get("/widgets").mock(
            side_effect=[
                httpx.Response(200, json=WIDGETS_PAGE_1),
                httpx.Response(200, json=WIDGETS_PAGE_2),
                httpx.Response(200, json=WIDGETS_PAGE_3),
            ]
        )

        widgets = list(paginate(bc, "/widgets", envelope_key="widgets", model=Widget))

        assert [w.size for w in widgets] == ["large", "small", "medium"]
        assert all(isinstance(w, Widget) for w in widgets)

    def test_cursor_is_passed_as_before_param(self, bc, api):
        route = api.get("/widgets").mock(
            side_effect=[
                httpx.Response(200, json=WIDGETS_PAGE_1),
                httpx.Response(200, json=WIDGETS_PAGE_3),
            ]
        )

        list(paginate(bc, "/widgets", envelope_key="widgets", model=Widget))

        first, second = route.calls
        assert "before" not in httpx.URL(str(first.request.url)).params
        assert (
            httpx.URL(str(second.request.url)).params["before"]
            == "019e7750-66ee-7001-8000-000000000001"
        )

    def test_lazy_page_one_only(self, bc, api):
        """Consuming only the first page's items must not fetch the second page."""
        route = api.get("/widgets").mock(
            side_effect=[
                httpx.Response(200, json=WIDGETS_PAGE_1),
                httpx.Response(200, json=WIDGETS_PAGE_3),
            ]
        )

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)
        next(iterator)  # the only item on page 1

        assert route.call_count == 1
        list(iterator)  # drain so the page-2 response is consumed too

    def test_lazy_crossing_the_boundary_fetches_the_next_page(self, bc, api):
        route = api.get("/widgets").mock(
            side_effect=[
                httpx.Response(200, json=WIDGETS_PAGE_1),
                httpx.Response(200, json=WIDGETS_PAGE_3),
            ]
        )

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)
        next(iterator)
        assert route.call_count == 1
        next(iterator)  # crosses into page 2
        assert route.call_count == 2

    def test_nothing_is_fetched_until_iteration_starts(self, bc, api):
        route = api.get("/widgets").respond(200, json=WIDGETS_PAGE_3)

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)
        assert route.call_count == 0  # creating the iterator fetched nothing

        list(iterator)
        assert route.call_count == 1  # iteration is what triggers the fetch

    def test_empty_first_page(self, bc, api):
        api.get("/widgets").respond(200, json={"widgets": [], "next_cursor": None})

        assert list(paginate(bc, "/widgets", envelope_key="widgets", model=Widget)) == []

    def test_extra_params_compose_with_the_cursor(self, bc, api):
        route = api.get("/widgets").mock(
            side_effect=[
                httpx.Response(200, json=WIDGETS_PAGE_1),
                httpx.Response(200, json=WIDGETS_PAGE_3),
            ]
        )

        list(
            paginate(bc, "/widgets", envelope_key="widgets", model=Widget, params={"size": "large"})
        )

        first, second = route.calls
        assert httpx.URL(str(first.request.url)).params["size"] == "large"
        second_params = httpx.URL(str(second.request.url)).params
        assert second_params["size"] == "large"
        assert "before" in second_params

    def test_items_carry_the_client(self, bc, api):
        api.get("/widgets").respond(200, json=WIDGETS_PAGE_3)

        (widget,) = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)

        assert widget._client is bc


class TestTotalCount:
    """``total_count``: the whole list's size, readable without consuming the iterator."""

    def test_is_a_list_iterator(self, bc, api):
        assert isinstance(
            paginate(bc, "/widgets", envelope_key="widgets", model=Widget), ListIterator
        )

    def test_reads_the_lists_total(self, bc, api):
        api.get("/widgets").respond(200, json=WIDGETS_PAGE_1)

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)

        assert iterator.total_count == 3

    def test_reading_it_first_consumes_nothing_and_page_one_is_not_fetched_again(self, bc, api):
        route = api.get("/widgets").mock(
            side_effect=[
                httpx.Response(200, json=WIDGETS_PAGE_1),
                httpx.Response(200, json=WIDGETS_PAGE_2),
                httpx.Response(200, json=WIDGETS_PAGE_3),
            ]
        )

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)
        assert iterator.total_count == 3
        assert route.call_count == 1

        assert [w.size for w in iterator] == ["large", "small", "medium"]
        assert route.call_count == 3  # page 1 was kept, not fetched twice

    def test_stays_readable_across_pages_without_fetching(self, bc, api):
        route = api.get("/widgets").mock(
            side_effect=[
                httpx.Response(200, json=WIDGETS_PAGE_1),
                httpx.Response(200, json=WIDGETS_PAGE_2),
                httpx.Response(200, json=WIDGETS_PAGE_3),
            ]
        )

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)
        next(iterator)
        next(iterator)  # now on page 2
        assert iterator.total_count == 3
        assert route.call_count == 2  # reading it fetched nothing more

        list(iterator)
        assert iterator.total_count == 3  # and after the end, still there
        assert route.call_count == 3

    def test_reports_what_the_latest_page_said(self, bc, api):
        """A record created while you page moves the count; the newest page's word stands."""
        api.get("/widgets").mock(
            side_effect=[
                httpx.Response(200, json=WIDGETS_PAGE_1),
                httpx.Response(200, json={**WIDGETS_PAGE_3, "total_count": 4}),
            ]
        )

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)
        assert iterator.total_count == 3
        list(iterator)
        assert iterator.total_count == 4

    def test_is_counted_under_the_filters(self, bc, api):
        route = api.get("/widgets").respond(200, json={**WIDGETS_PAGE_3, "total_count": 1})

        iterator = paginate(
            bc, "/widgets", envelope_key="widgets", model=Widget, params={"size": "medium"}
        )

        assert iterator.total_count == 1
        assert route.calls.last.request.url.params["size"] == "medium"

    def test_an_empty_list_counts_zero(self, bc, api):
        api.get("/widgets").respond(
            200, json={"widgets": [], "next_cursor": None, "total_count": 0}
        )

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)

        assert iterator.total_count == 0
        assert list(iterator) == []

    def test_a_page_without_it_still_iterates_but_has_no_count_to_read(self, bc, api):
        """The SDK never invents a count the API withheld — and never needs one to iterate."""
        api.get("/widgets").respond(200, json={"widgets": [], "next_cursor": None})

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)

        assert list(iterator) == []
        with pytest.raises(AttributeError, match="total_count"):
            iterator.total_count

    def test_a_failed_page_can_be_retried_by_iterating_again(self, bc, api):
        """A page that failed fetched nothing into the iterator: the next call asks again."""
        api.get("/widgets").mock(
            side_effect=[httpx.ConnectError("down"), httpx.Response(200, json=WIDGETS_PAGE_3)]
        )

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)
        with pytest.raises(BaseCradleError):
            next(iterator)

        assert [w.size for w in iterator] == ["medium"]

    def test_a_page_that_fails_to_read_keeps_nothing_so_a_retry_has_no_duplicates(self, bc, api):
        """Every row is read before any is kept: a page that fails partway buffers none."""

        class Picky(Widget):
            def __init__(self, data, *, client=None):
                if data.get("size") == "unreadable":
                    raise ValueError("unreadable row")
                super().__init__(data, client=client)

        unreadable = {
            "widgets": [
                *WIDGETS_PAGE_3["widgets"],
                {"uuid": WIDGETS_PAGE_1["next_cursor"], "size": "unreadable"},
            ],
            "next_cursor": None,
            "total_count": 2,
        }
        api.get("/widgets").mock(
            side_effect=[
                httpx.Response(200, json=unreadable),
                httpx.Response(200, json=WIDGETS_PAGE_3),
            ]
        )

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Picky)
        with pytest.raises(ValueError, match="unreadable"):
            next(iterator)

        assert [w.size for w in iterator] == ["medium"]

    def test_a_page_without_a_cursor_is_the_last_page(self, bc, api):
        """The spec declares ``next_cursor`` on ``GET /users`` without requiring it."""
        route = api.get("/widgets").respond(
            200, json={"widgets": WIDGETS_PAGE_1["widgets"], "total_count": 1}
        )

        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)

        assert [w.size for w in iterator] == ["large"]
        assert iterator.total_count == 1
        assert route.call_count == 1

    def test_repr_names_the_list_and_never_the_client(self, bc):
        iterator = paginate(
            bc, "/widgets", envelope_key="widgets", model=Widget, params={"size": "large"}
        )

        assert repr(iterator) == "<ListIterator path='/widgets' filters={'size': 'large'}>"

    def test_refuses_to_be_pickled(self, bc):
        iterator = paginate(bc, "/widgets", envelope_key="widgets", model=Widget)

        with pytest.raises(BaseCradleError, match="cannot be serialized"):
            pickle.dumps(iterator)


@pytest.mark.anyio
class TestAsyncTotalCount:
    """The same, on the async client: ``await iterator.total_count``."""

    async def test_is_an_async_list_iterator(self, abc):
        iterator = apaginate(abc, "/widgets", envelope_key="widgets", model=Widget)

        assert isinstance(iterator, AsyncListIterator)

    async def test_reading_it_first_consumes_nothing(self, abc, api):
        route = api.get("/widgets").mock(
            side_effect=[
                httpx.Response(200, json=WIDGETS_PAGE_1),
                httpx.Response(200, json=WIDGETS_PAGE_2),
                httpx.Response(200, json=WIDGETS_PAGE_3),
            ]
        )

        iterator = apaginate(abc, "/widgets", envelope_key="widgets", model=Widget)
        assert await iterator.total_count == 3
        assert route.call_count == 1

        assert [w.size async for w in iterator] == ["large", "small", "medium"]
        assert route.call_count == 3
        assert await iterator.total_count == 3  # after the end: no fetch
        assert route.call_count == 3

    async def test_concurrent_first_reads_fetch_page_one_once(self):
        """``gather`` of the total and the first record must not buffer page 1 twice.

        A stub client, not respx: its request suspends at an ``await``, which a respx mock
        never does, so without it the two reads would never interleave and the test would
        pass with or without the guard.
        """

        class SlowClient:
            def __init__(self, pages):
                self.pages = list(pages)
                self.calls = 0

            async def request(self, method, path, *, params):
                self.calls += 1
                await asyncio.sleep(0)  # let the other task run mid-fetch
                return self.pages.pop(0)

        client = SlowClient([WIDGETS_PAGE_1, WIDGETS_PAGE_3])
        iterator = apaginate(client, "/widgets", envelope_key="widgets", model=Widget)

        total, first = await asyncio.gather(iterator.total_count, iterator.__anext__())

        assert total == 3
        assert first.size == "large"
        assert client.calls == 1
        assert [w.size async for w in iterator] == ["medium"]

    async def test_a_failed_page_can_be_retried_by_iterating_again(self, abc, api):
        api.get("/widgets").mock(
            side_effect=[httpx.ConnectError("down"), httpx.Response(200, json=WIDGETS_PAGE_3)]
        )

        iterator = apaginate(abc, "/widgets", envelope_key="widgets", model=Widget)
        with pytest.raises(BaseCradleError):
            await iterator.__anext__()

        assert [w.size async for w in iterator] == ["medium"]
        assert await iterator.total_count == 3

    async def test_a_page_without_it_has_no_count_to_read(self, abc, api):
        api.get("/widgets").respond(200, json={"widgets": [], "next_cursor": None})

        iterator = apaginate(abc, "/widgets", envelope_key="widgets", model=Widget)

        with pytest.raises(AttributeError, match="total_count"):
            await iterator.total_count
