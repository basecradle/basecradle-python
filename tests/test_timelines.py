"""Timelines: auto-paginating list, create, get, lock, participants."""

import json
import typing

import httpx
import pytest

from basecradle import (
    ApiObject,
    Asset,
    AssetContent,
    ForbiddenError,
    Message,
    MessageContent,
    NotFoundError,
    NotTimelineOwnerError,
    Task,
    TaskContent,
    Timeline,
    TimelineItem,
    User,
    ValidationError,
    WebhookEndpoint,
    WebhookEvent,
    WebhookEventContent,
    WebhookEventHeaders,
)
from basecradle._timelines import _CONTENT_CLASSES
from tests.conftest import (
    NOVA,
    TIMELINE_UUID,
    WEBHOOK_ENDPOINT_UUID,
    WEBHOOK_EVENT_UUID,
    asset_payload,
    lock_response,
    message_payload,
    participation_response,
    problem,
    task_payload,
    timeline_payload,
    webhook_event_payload,
)

# One shape per record, everywhere it appears: an inline item *is* the record's own form
# (only ``created_at`` differs — when it landed on the timeline), so the item fixtures are
# the record fixtures.
MESSAGE_ITEM = message_payload()


class TestIteration:
    def test_iterates_all_pages_newest_first(self, bc, api):
        page_one = timeline_payload(uuid="019e7750-66ee-7f53-829f-13a8a710b6da", name="Newer")
        page_two = timeline_payload(uuid="019e7750-66ee-7611-8e63-26d6c2a2c6f5", name="Older")
        api.get("/timelines").mock(
            side_effect=[
                httpx.Response(
                    200, json={"timelines": [page_one], "next_cursor": page_one["uuid"]}
                ),
                httpx.Response(200, json={"timelines": [page_two], "next_cursor": None}),
            ]
        )

        timelines = list(bc.timelines)

        assert [t.name for t in timelines] == ["Newer", "Older"]
        assert all(isinstance(t, Timeline) for t in timelines)

    def test_rows_have_typed_owner_and_participants(self, bc, api):
        api.get("/timelines").respond(
            200, json={"timelines": [timeline_payload()], "next_cursor": None}
        )

        (timeline,) = bc.timelines

        assert isinstance(timeline.owner, User)
        assert timeline.owner.handle == "john"
        assert isinstance(timeline.participants[0], User)
        assert timeline.participants[0].handle == "nova"
        assert timeline.locked is False

    def test_list_rows_do_not_carry_items(self, bc, api):
        api.get("/timelines").respond(
            200, json={"timelines": [timeline_payload()], "next_cursor": None}
        )

        (timeline,) = bc.timelines

        with pytest.raises(AttributeError) as exc_info:
            timeline.items
        assert "items" in str(exc_info.value)


class TestCreate:
    def test_create_sends_rails_nested_body_and_returns_timeline(self, bc, api):
        route = api.post("/timelines").respond(
            201, json={"timeline": timeline_payload(participants=[]), "items": []}
        )

        timeline = bc.timelines.create(name="Incident response")

        assert json.loads(route.calls.last.request.read()) == {
            "timeline": {"name": "Incident response"}
        }
        assert isinstance(timeline, Timeline)
        assert timeline.name == "Incident response"
        assert timeline.items == []
        assert timeline.participants == []

    def test_create_over_cap_raises_validation_error(self, bc, api):
        api.post("/timelines").respond(
            422,
            json=problem(
                "validation_failed",
                422,
                detail="Owner has reached the timeline limit",
                errors={"owner": ["has reached the timeline limit"]},
            ),
        )

        with pytest.raises(ValidationError) as exc_info:
            bc.timelines.create(name="One too many")

        assert exc_info.value.errors == {"owner": ["has reached the timeline limit"]}


class TestGet:
    def test_get_merges_the_two_key_envelope(self, bc, api):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200, json={"timeline": timeline_payload(), "items": [MESSAGE_ITEM]}
        )

        timeline = bc.timelines.get(TIMELINE_UUID)

        assert timeline.name == "Incident response"
        (item,) = timeline.items
        assert isinstance(item, TimelineItem)
        assert item.type == "message"
        assert isinstance(item.user, User)
        assert item.user.handle == "john"
        assert item.content.body == "Hello from a peer."

    def test_every_item_carries_its_own_timeline_reference_and_updated_at(self, bc, api):
        """An item is the record's own form: it points back at its container, and dates."""
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200,
            json={
                "timeline": timeline_payload(),
                "items": [MESSAGE_ITEM, webhook_event_payload()],
            },
        )

        message_item, event_item = bc.timelines.get(TIMELINE_UUID).items

        for item in (message_item, event_item):
            assert item.timeline.uuid == TIMELINE_UUID
            assert item.updated_at == "2026-01-02T00:00:00.000Z"

    def test_webhook_event_item_has_no_user(self, bc, api):
        """A ``webhook_event`` item has no author — an inbound delivery came from outside.

        Every other item keeps ``user``; only this one has none, and reading it raises
        rather than inventing a value. The delivery's *endpoint* does have an author.
        """
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200,
            json={
                "timeline": timeline_payload(),
                "items": [webhook_event_payload(), MESSAGE_ITEM],
            },
        )

        event_item, message_item = bc.timelines.get(TIMELINE_UUID).items

        assert event_item.type == "webhook_event"
        assert event_item.content.payload == '{"status":"ok"}'
        with pytest.raises(AttributeError) as exc_info:
            event_item.user
        assert "user" in str(exc_info.value)
        assert message_item.user.handle == "john"  # every other item keeps its author
        assert event_item.webhook_endpoint.user.handle == "john"  # the endpoint has one

    def test_webhook_event_item_embeds_its_endpoint_in_full(self, bc, api):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200,
            json={"timeline": timeline_payload(), "items": [webhook_event_payload()]},
        )

        (item,) = bc.timelines.get(TIMELINE_UUID).items

        assert isinstance(item.webhook_endpoint, WebhookEndpoint)
        assert item.webhook_endpoint.content.uuid == WEBHOOK_ENDPOINT_UUID
        assert item.webhook_endpoint.content.ingest_url.startswith("https://basecradle.com/")

    def test_get_as_non_viewer_raises_forbidden(self, bc, api):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(403, json=problem("not_a_viewer", 403))

        with pytest.raises(ForbiddenError):
            bc.timelines.get(TIMELINE_UUID)

    def test_get_unknown_uuid_raises_not_found(self, bc, api):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(404, json=problem("not_found", 404))

        with pytest.raises(NotFoundError):
            bc.timelines.get(TIMELINE_UUID)


class TestItemContentIsTyped:
    """One record has one shape: a ``timeline.items`` row gives the record's own content class.

    Before this, ``item.content`` was the generic ``ApiObject`` on every path, so the same
    wire field answered differently depending on how you reached it — the case-folding
    ``headers`` landed for ``bc.webhook_events`` and not for ``timeline.items``. These pin
    the promise that there is one answer.
    """

    def test_content_is_typed_by_the_items_own_type(self, bc, api):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200,
            json={
                "timeline": timeline_payload(),
                "items": [
                    message_payload(),
                    asset_payload(),
                    webhook_event_payload(),
                    task_payload(),
                ],
            },
        )

        message, asset, event, task = bc.timelines.get(TIMELINE_UUID).items

        assert isinstance(message.content, MessageContent)
        assert isinstance(asset.content, AssetContent)
        assert isinstance(event.content, WebhookEventContent)
        assert isinstance(task.content, TaskContent)
        # Still wire-exact underneath — typing the content changed nothing about the read.
        assert message.content.body == "Hello from a peer."
        assert asset.content.file.filename == "report.pdf"
        assert task.content.status == "pending"
        assert event.content.payload == '{"status":"ok"}'

    def test_webhook_event_item_headers_fold_case(self, bc, api):
        """The divergence that motivated this: ``headers`` now folds case on both paths."""
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200,
            json={"timeline": timeline_payload(), "items": [webhook_event_payload()]},
        )

        (item,) = bc.timelines.get(TIMELINE_UUID).items

        assert isinstance(item.content.headers, WebhookEventHeaders)
        assert item.content.headers["X-Example-Event"] == "ping"  # the wire's own spelling
        assert item.content.headers["x-example-event"] == "ping"  # any other casing
        assert "X-EXAMPLE-EVENT" in item.content.headers
        assert item.content.headers.get("x-example-event") == "ping"

    def test_item_content_equals_the_same_record_fetched_directly(self, bc, api):
        """``ApiObject.__eq__`` compares types, so this only holds once the item is typed."""
        event = webhook_event_payload()
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200, json={"timeline": timeline_payload(), "items": [event]}
        )
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(200, json={"webhook_event": event})

        (item,) = bc.timelines.get(TIMELINE_UUID).items
        directly = bc.webhook_events.get(WEBHOOK_EVENT_UUID)

        assert item.content == directly.content
        assert type(item.content) is type(directly.content)

    @pytest.mark.parametrize(
        ("item_type", "model"),
        [("message", Message), ("asset", Asset), ("webhook_event", WebhookEvent), ("task", Task)],
    )
    def test_the_table_agrees_with_the_records_own_annotation(self, item_type, model):
        """The item path and the record path must not be able to drift apart again.

        ``_CONTENT_CLASSES`` is a second declaration of a fact the leaf models already
        state as ``content: SomeContent``. Retype one of those and the two paths would
        silently diverge for the new field \u2014 which is exactly the bug this change closed,
        and every other test here would still pass, because they check the item path
        against the table's own answer. So assert the two declarations *are* the same class.
        """
        assert _CONTENT_CLASSES[item_type] is typing.get_type_hints(model)["content"]

    def test_the_table_covers_every_item_type_and_nothing_else(self):
        """The four the API defines \u2014 spec-checked (``items[].content`` is their union)."""
        assert set(_CONTENT_CLASSES) == {"message", "asset", "webhook_event", "task"}

    def test_unknown_item_type_reads_as_the_generic_object(self, bc, api):
        """The API is additive-only: an item type added after this release must keep reading.

        No raise, no empty content — the generic wire-exact object, whose fields are
        readable immediately. That is the same guarantee ``ApiObject`` makes everywhere.
        """
        future_item = {
            "type": "poll",
            "created_at": "2026-01-02T00:00:00.000Z",
            "updated_at": "2026-01-02T00:00:00.000Z",
            "user": NOVA,
            "timeline": {"uuid": TIMELINE_UUID},
            "content": {"uuid": "019e7750-66ee-7c0d-9a1f-4f0d2b9a7e51", "question": "Ship it?"},
        }
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200, json={"timeline": timeline_payload(), "items": [future_item]}
        )

        (item,) = bc.timelines.get(TIMELINE_UUID).items

        assert item.type == "poll"
        assert type(item.content) is ApiObject
        assert item.content.question == "Ship it?"

    def test_an_item_with_no_type_still_reads_its_content(self, bc, api):
        """A row missing ``type`` is malformed, but ``content`` owes no exception for it."""
        item_without_type = {
            "created_at": "2026-01-02T00:00:00.000Z",
            "updated_at": "2026-01-02T00:00:00.000Z",
            "timeline": {"uuid": TIMELINE_UUID},
            "content": {"uuid": "019e7750-66ee-7c0d-9a1f-4f0d2b9a7e51"},
        }
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200, json={"timeline": timeline_payload(), "items": [item_without_type]}
        )

        (item,) = bc.timelines.get(TIMELINE_UUID).items

        assert type(item.content) is ApiObject
        assert item.content.uuid == "019e7750-66ee-7c0d-9a1f-4f0d2b9a7e51"


class TestLock:
    def test_lock_posts_and_updates_local_state(self, bc, api):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200, json={"timeline": timeline_payload(locked=False), "items": []}
        )
        lock_route = api.post(f"/timelines/{TIMELINE_UUID}/lock").respond(200, json=lock_response())

        timeline = bc.timelines.get(TIMELINE_UUID)
        assert timeline.locked is False

        timeline.lock()

        assert lock_route.called
        assert timeline.locked is True  # live object: updated from the API's response

    def test_lock_adopts_the_whole_returned_timeline(self, bc, api):
        """The response is the timeline in subject form — every field is adopted."""
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200,
            json={
                "timeline": timeline_payload(locked=False, updated_at="2026-01-02T00:00:00.000Z"),
                "items": [MESSAGE_ITEM],
            },
        )
        api.post(f"/timelines/{TIMELINE_UUID}/lock").mock(
            return_value=httpx.Response(
                200,
                json={
                    "timeline": timeline_payload(
                        locked=True, name="Renamed", updated_at="2026-01-03T00:00:00.000Z"
                    )
                },
            )
        )

        timeline = bc.timelines.get(TIMELINE_UUID)
        timeline.lock()

        assert timeline.locked is True
        assert timeline.name == "Renamed"
        assert timeline.updated_at == "2026-01-03T00:00:00.000Z"
        # ``items`` is the SDK's own key and the lock response does not speak to it —
        # locking freezes content, so what was already read stays readable.
        assert [item.type for item in timeline.items] == ["message"]

    def test_lock_is_idempotent(self, bc, api):
        api.get("/timelines").respond(
            200, json={"timelines": [timeline_payload(locked=True)], "next_cursor": None}
        )
        api.post(f"/timelines/{TIMELINE_UUID}/lock").respond(200, json=lock_response())

        (timeline,) = bc.timelines
        timeline.lock()  # locking an already-locked timeline succeeds

        assert timeline.locked is True

    def test_lock_as_non_viewer_raises_forbidden(self, bc, api):
        api.get("/timelines").respond(
            200, json={"timelines": [timeline_payload()], "next_cursor": None}
        )
        api.post(f"/timelines/{TIMELINE_UUID}/lock").respond(403, json=problem("not_a_viewer", 403))

        (timeline,) = bc.timelines
        with pytest.raises(ForbiddenError):
            timeline.lock()


class TestDelete:
    @pytest.fixture
    def timeline(self, bc, api):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200, json={"timeline": timeline_payload(), "items": []}
        )
        return bc.timelines.get(TIMELINE_UUID)

    def test_delete_sends_delete_and_returns_none(self, bc, api, timeline):
        route = api.delete(f"/timelines/{TIMELINE_UUID}").respond(204)

        result = timeline.delete()

        assert route.called
        assert result is None

    def test_delete_of_locked_timeline_succeeds(self, bc, api):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200, json={"timeline": timeline_payload(locked=True), "items": []}
        )
        route = api.delete(f"/timelines/{TIMELINE_UUID}").respond(204)

        timeline = bc.timelines.get(TIMELINE_UUID)
        assert timeline.delete() is None  # locking freezes content, not governance
        assert route.called

    def test_delete_as_non_owner_raises_forbidden(self, bc, api, timeline):
        api.delete(f"/timelines/{TIMELINE_UUID}").respond(
            403, json=problem("not_timeline_owner", 403)
        )

        with pytest.raises(NotTimelineOwnerError):
            timeline.delete()

    def test_delete_unknown_uuid_raises_not_found(self, bc, api, timeline):
        api.delete(f"/timelines/{TIMELINE_UUID}").respond(404, json=problem("not_found", 404))

        with pytest.raises(NotFoundError):
            timeline.delete()


class TestAddParticipant:
    @pytest.fixture
    def timeline(self, bc, api):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200, json={"timeline": timeline_payload(participants=[]), "items": []}
        )
        return bc.timelines.get(TIMELINE_UUID)

    def test_add_by_user_object(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/participations").respond(
            201, json=participation_response()
        )

        added = timeline.add_participant(User(NOVA))

        assert json.loads(route.calls.last.request.read()) == {"user_id": NOVA["uuid"]}
        assert isinstance(added, User)
        assert added.handle == "nova"

    def test_add_by_uuid_string(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/participations").respond(
            201, json=participation_response()
        )

        timeline.add_participant(NOVA["uuid"])

        assert json.loads(route.calls.last.request.read()) == {"user_id": NOVA["uuid"]}

    def test_add_appends_to_local_participants(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/participations").respond(
            201, json=participation_response()
        )

        assert timeline.participants == []
        added = timeline.add_participant(NOVA["uuid"])

        assert added.handle == "nova"
        assert [p.handle for p in timeline.participants] == ["nova"]

    def test_add_appends_the_user_not_the_envelope(self, bc, api, timeline):
        """The ``{"user": ...}`` envelope is unwrapped before it lands — no nesting."""
        api.post(f"/timelines/{TIMELINE_UUID}/participations").respond(
            201, json=participation_response()
        )

        timeline.add_participant(NOVA["uuid"])

        (participant,) = timeline.participants
        assert participant.uuid == NOVA["uuid"]
        assert participant.trust.mutual is True  # the subject form's own trust block

    def test_idempotent_add_does_not_duplicate_locally(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/participations").respond(
            201, json=participation_response()
        )

        timeline.add_participant(NOVA["uuid"])
        timeline.add_participant(NOVA["uuid"])  # idempotent on the API side too

        assert len(timeline.participants) == 1

    def test_add_without_mutual_trust_raises_validation_error(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/participations").respond(
            422,
            json=problem(
                "validation_failed",
                422,
                detail="Mutual trust is not established",
                errors={"user": ["must have mutual trust with every viewer"]},
            ),
        )

        with pytest.raises(ValidationError):
            timeline.add_participant(NOVA["uuid"])

    def test_add_as_non_owner_raises_forbidden(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/participations").respond(
            403, json=problem("not_timeline_owner", 403)
        )

        with pytest.raises(ForbiddenError):
            timeline.add_participant(NOVA["uuid"])


class TestRemoveParticipant:
    @pytest.fixture
    def timeline(self, bc, api):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200,
            json={"timeline": timeline_payload(), "items": []},  # Nova participates
        )
        return bc.timelines.get(TIMELINE_UUID)

    def test_remove_by_user_object(self, bc, api, timeline):
        route = api.delete(f"/timelines/{TIMELINE_UUID}/participations/{NOVA['uuid']}").respond(204)

        result = timeline.remove_participant(User(NOVA))

        assert route.called
        assert result is None

    def test_remove_updates_local_participants(self, bc, api, timeline):
        api.delete(f"/timelines/{TIMELINE_UUID}/participations/{NOVA['uuid']}").respond(204)

        assert [p.handle for p in timeline.participants] == ["nova"]
        timeline.remove_participant(NOVA["uuid"])

        assert timeline.participants == []

    def test_remove_is_idempotent(self, bc, api, timeline):
        api.delete(f"/timelines/{TIMELINE_UUID}/participations/{NOVA['uuid']}").respond(204)

        timeline.remove_participant(NOVA["uuid"])
        timeline.remove_participant(NOVA["uuid"])  # removing a non-participant is still 204

        assert timeline.participants == []

    def test_remove_as_non_owner_raises_forbidden(self, bc, api, timeline):
        api.delete(f"/timelines/{TIMELINE_UUID}/participations/{NOVA['uuid']}").respond(
            403, json=problem("not_timeline_owner", 403)
        )

        with pytest.raises(ForbiddenError):
            timeline.remove_participant(NOVA["uuid"])


class TestDetachedObjects:
    def test_verbs_require_a_client(self):
        timeline = Timeline(timeline_payload())  # built by hand, no client

        with pytest.raises(RuntimeError) as exc_info:
            timeline.lock()

        assert "not attached to a BaseCradle client" in str(exc_info.value)
