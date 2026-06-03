"""Sync/async parity, pinned as a permanent invariant.

Issue #12's acceptance criterion — "full resource parity between sync and async clients" —
is not a one-time check. These tests fail if a future resource ships in one client but not
the other, or if the two ever stop sharing model classes.
"""

import inspect

import pytest

from basecradle import (
    AsyncBaseCradle,
    BaseCradle,
    Session,
    Timeline,
    User,
    WebhookEndpoint,
)
from basecradle._items import AsyncItemsResource, ItemsResource
from tests.conftest import FAKE_TOKEN

RESOURCE_NAMES = (
    "timelines",
    "messages",
    "assets",
    "tasks",
    "webhook_endpoints",
    "webhook_events",
    "sessions",
    "users",
)


@pytest.fixture
def both_clients():
    sync_client = BaseCradle(token=FAKE_TOKEN)
    async_client = AsyncBaseCradle(token=FAKE_TOKEN)
    yield sync_client, async_client
    sync_client.close()
    # The async client's pool is closed by garbage collection here; aclose() needs a loop
    # and these are sync tests. Constructing without requests leaves nothing open anyway.


class TestClientParity:
    def test_both_clients_expose_the_same_resources(self, both_clients):
        sync_client, async_client = both_clients

        for name in RESOURCE_NAMES:
            assert hasattr(sync_client, name), f"BaseCradle lost bc.{name}"
            assert hasattr(async_client, name), f"AsyncBaseCradle is missing abc.{name}"

    def test_no_resource_exists_in_only_one_client(self, both_clients):
        """If someone adds a resource to one client, this fails until it's in both."""
        sync_client, async_client = both_clients

        def resource_names(client):
            return {
                name
                for name, value in vars(client).items()
                if not name.startswith("_") and "Resource" in type(value).__name__
            }

        assert resource_names(sync_client) == resource_names(async_client)

    def test_both_clients_share_construction_semantics(self, monkeypatch):
        monkeypatch.delenv("BASECRADLE_TOKEN", raising=False)
        from basecradle import MissingTokenError

        for cls in (BaseCradle, AsyncBaseCradle):
            with pytest.raises(MissingTokenError):
                cls()

    def test_login_signatures_match(self):
        sync_params = inspect.signature(BaseCradle.login).parameters
        async_params = inspect.signature(AsyncBaseCradle.login).parameters
        assert list(sync_params) == list(async_params)

    def test_request_signatures_match(self):
        sync_params = inspect.signature(BaseCradle.request).parameters
        async_params = inspect.signature(AsyncBaseCradle.request).parameters
        assert list(sync_params) == list(async_params)


class TestModelParity:
    """The same model classes serve both clients — there are no Async* model twins."""

    def test_models_are_shared_not_duplicated(self):
        import basecradle

        async_names = [name for name in basecradle.__all__ if name.startswith("Async")]
        model_twins = [
            name
            for name in async_names
            if name.removeprefix("Async") in ("Timeline", "User", "Session", "WebhookEndpoint")
        ]
        assert model_twins == [], f"Async model twins must not exist: {model_twins}"

    def test_verb_carrying_models_have_no_async_variants(self):
        import basecradle

        for model in (Timeline, User, Session, WebhookEndpoint):
            assert not hasattr(basecradle, f"Async{model.__name__}")


class TestResourceMethodParity:
    """Every public sync resource method exists on the async counterpart."""

    SYNC_ASYNC_PAIRS = [
        ("TimelinesResource", "AsyncTimelinesResource"),
        ("MessagesResource", "AsyncMessagesResource"),
        ("AssetsResource", "AsyncAssetsResource"),
        ("TasksResource", "AsyncTasksResource"),
        ("WebhookEndpointsResource", "AsyncWebhookEndpointsResource"),
        ("WebhookEventsResource", "AsyncWebhookEventsResource"),
        ("SessionsResource", "AsyncSessionsResource"),
        ("UsersResource", "AsyncUsersResource"),
    ]

    @pytest.mark.parametrize(("sync_name", "async_name"), SYNC_ASYNC_PAIRS)
    def test_public_methods_match(self, sync_name, async_name):
        import basecradle

        sync_class = getattr(basecradle, sync_name)
        async_class = getattr(basecradle, async_name)

        def public_methods(cls):
            return {
                name
                for name, member in inspect.getmembers(cls, callable)
                if not name.startswith("_")
            }

        missing = public_methods(sync_class) - public_methods(async_class)
        extra = public_methods(async_class) - public_methods(sync_class)
        assert not missing, f"{async_name} is missing: {missing}"
        assert not extra, f"{async_name} has methods {sync_name} lacks: {extra}"

    @pytest.mark.parametrize(("sync_name", "async_name"), SYNC_ASYNC_PAIRS)
    def test_iteration_protocols(self, sync_name, async_name):
        import basecradle

        assert hasattr(getattr(basecradle, sync_name), "__iter__")
        assert hasattr(getattr(basecradle, async_name), "__aiter__")


class TestSharedCore:
    """The acceptance criterion's other half: no copy-pasted business logic."""

    def test_items_resources_share_one_core(self):
        from basecradle._items import _ItemsResourceCore

        assert issubclass(ItemsResource, _ItemsResourceCore)
        assert issubclass(AsyncItemsResource, _ItemsResourceCore)

    def test_bindings_are_shared_between_sync_and_async(self):
        """Path/envelope/model bindings are declared once and inherited by both."""
        from basecradle import AsyncMessagesResource, MessagesResource

        assert MessagesResource._path == AsyncMessagesResource._path
        assert MessagesResource._model is AsyncMessagesResource._model

        sync_binding = MessagesResource.__mro__[1]
        async_binding = AsyncMessagesResource.__mro__[1]
        assert sync_binding is async_binding  # literally the same class

    def test_error_mapping_is_shared(self):
        """Both clients raise from the same exception factory."""
        from basecradle._client import _ClientCore

        assert BaseCradle._check_response is _ClientCore._check_response
        assert AsyncBaseCradle._check_response is _ClientCore._check_response
