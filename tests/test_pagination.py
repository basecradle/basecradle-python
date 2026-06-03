"""The shared pagination engine — generic, lazy, cursor-invisible.

These tests use a throwaway model and envelope key on purpose: the engine must be
genuinely generic (acceptance criterion: the next list resource reuses it unmodified).
"""

import httpx

from basecradle import ApiObject
from basecradle._pagination import paginate

# A deliberately non-Timeline resource, to pin genericity.
WIDGETS_PAGE_1 = {
    "widgets": [{"uuid": "019e7750-66ee-7001-8000-000000000001", "size": "large"}],
    "next_cursor": "019e7750-66ee-7001-8000-000000000001",
}
WIDGETS_PAGE_2 = {
    "widgets": [{"uuid": "019e7750-66ee-7002-8000-000000000002", "size": "small"}],
    "next_cursor": "019e7750-66ee-7002-8000-000000000002",
}
WIDGETS_PAGE_3 = {
    "widgets": [{"uuid": "019e7750-66ee-7003-8000-000000000003", "size": "medium"}],
    "next_cursor": None,
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
