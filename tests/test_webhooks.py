"""Webhook endpoints & events: create, enable/disable, rotate, and the read-only event feed."""

import json

import httpx
import pytest

from basecradle import (
    ForbiddenError,
    NotFoundError,
    TimelineLockedError,
    ValidationError,
    WebhookEndpoint,
    WebhookEvent,
    WebhookVerification,
)
from tests.conftest import (
    TIMELINE_UUID,
    WEBHOOK_ENDPOINT_UUID,
    WEBHOOK_EVENT_UUID,
    problem,
    timeline_payload,
    webhook_endpoint_payload,
    webhook_event_payload,
)

ROTATED_INGEST_URL = "https://basecradle.com/webhooks/019e7750-66ee-7eb8-b8a8-e882e4d6e2a9"


@pytest.fixture
def timeline(bc, api):
    api.get(f"/timelines/{TIMELINE_UUID}").respond(
        200, json={"timeline": timeline_payload(), "items": []}
    )
    return bc.timelines.get(TIMELINE_UUID)


@pytest.fixture
def endpoint(bc, api, timeline):
    api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
        201, json={"webhook_endpoint": webhook_endpoint_payload()}
    )
    return timeline.webhook_endpoints.create(description="CI deploys")


class TestEndpointLifecycle:
    def test_create_disable_enable_rotate_end_to_end(self, bc, api, timeline):
        """The full lifecycle from the issue's acceptance criteria, against mocks."""
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload(enabled=True)}
        )
        enablement = api.route(
            method__in=["POST", "DELETE"],
            path=f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/enablement",
        ).mock(
            side_effect=[
                httpx.Response(
                    200, json={"webhook_endpoint": webhook_endpoint_payload(enabled=False)}
                ),
                httpx.Response(
                    200, json={"webhook_endpoint": webhook_endpoint_payload(enabled=True)}
                ),
            ]
        )
        api.post(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/rotation").respond(
            200,
            json={
                "webhook_endpoint": webhook_endpoint_payload(
                    enabled=True, ingest_url=ROTATED_INGEST_URL
                )
            },
        )

        # create
        endpoint = timeline.webhook_endpoints.create(description="CI deploys")
        assert endpoint.content.enabled is True
        original_url = endpoint.content.ingest_url

        # disable → enable
        endpoint.disable()
        assert endpoint.content.enabled is False
        endpoint.enable()
        assert endpoint.content.enabled is True
        assert enablement.call_count == 2

        # rotate: same identity, new secret URL
        endpoint.rotate()
        assert endpoint.content.uuid == WEBHOOK_ENDPOINT_UUID
        assert endpoint.content.ingest_url == ROTATED_INGEST_URL
        assert endpoint.content.ingest_url != original_url

    def test_disable_uses_delete_on_enablement(self, bc, api, endpoint):
        route = api.delete(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/enablement").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload(enabled=False)}
        )

        endpoint.disable()

        assert route.called
        assert endpoint.content.enabled is False

    def test_enable_uses_post_on_enablement(self, bc, api, endpoint):
        route = api.post(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/enablement").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload(enabled=True)}
        )

        endpoint.enable()

        assert route.called
        assert endpoint.content.enabled is True

    def test_rotate_uses_post_on_rotation(self, bc, api, endpoint):
        route = api.post(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/rotation").respond(
            200,
            json={"webhook_endpoint": webhook_endpoint_payload(ingest_url=ROTATED_INGEST_URL)},
        )

        endpoint.rotate()

        assert route.called
        assert endpoint.content.ingest_url == ROTATED_INGEST_URL

    def test_verbs_as_non_viewer_raise_forbidden(self, bc, api, endpoint):
        api.delete(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/enablement").respond(
            403, json=problem("not_a_viewer", 403)
        )

        with pytest.raises(ForbiddenError):
            endpoint.disable()


class TestEndpointCreate:
    def test_create_sends_rails_nested_body(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint = timeline.webhook_endpoints.create(description="CI deploys")

        assert json.loads(route.calls.last.request.read()) == {
            "webhook_endpoint": {"description": "CI deploys"}
        }
        assert isinstance(endpoint, WebhookEndpoint)
        assert endpoint.content.description == "CI deploys"
        assert endpoint.content.ingest_url.startswith("https://basecradle.com/webhooks/")

    def test_create_with_idempotency_key_sends_header(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        timeline.webhook_endpoints.create(description="CI deploys", idempotency_key="key-3")

        assert route.calls.last.request.headers["Idempotency-Key"] == "key-3"

    def test_create_without_idempotency_key_sends_no_header(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        timeline.webhook_endpoints.create(description="CI deploys")

        assert "Idempotency-Key" not in route.calls.last.request.headers

    def test_verification_block_is_typed(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint = timeline.webhook_endpoints.create(description="CI deploys")

        assert isinstance(endpoint.content.verification, WebhookVerification)
        assert endpoint.content.verification.enabled is False
        assert endpoint.content.verification.signature_header == "X-Signature"
        assert endpoint.content.verification.verifier == "hmac_sha256_hex"

    def test_create_on_locked_timeline_raises(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            403, json=problem("timeline_locked", 403)
        )

        with pytest.raises(TimelineLockedError):
            timeline.webhook_endpoints.create(description="CI deploys")

    def test_create_blank_description_raises(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            422,
            json=problem("validation_failed", 422, errors={"description": ["can't be blank"]}),
        )

        with pytest.raises(ValidationError) as exc_info:
            timeline.webhook_endpoints.create(description="")

        assert exc_info.value.errors == {"description": ["can't be blank"]}

    def test_endpoints_have_no_user(self, bc, api, timeline):
        """Endpoints belong to the timeline, not a user — no user block, by design."""
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint = timeline.webhook_endpoints.create(description="CI deploys")

        with pytest.raises(AttributeError):
            endpoint.user


class TestEndpointsResource:
    def test_iteration_paginates(self, bc, api):
        api.get("/webhook_endpoints").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "webhook_endpoints": [webhook_endpoint_payload()],
                        "next_cursor": "019e7750-66ee-7611-8e63-26d6c2a2c6f5",
                    },
                ),
                httpx.Response(
                    200,
                    json={"webhook_endpoints": [webhook_endpoint_payload()], "next_cursor": None},
                ),
            ]
        )

        endpoints = list(bc.webhook_endpoints)

        assert len(endpoints) == 2
        assert all(isinstance(e, WebhookEndpoint) for e in endpoints)

    def test_get(self, bc, api):
        api.get(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint = bc.webhook_endpoints.get(WEBHOOK_ENDPOINT_UUID)

        assert isinstance(endpoint, WebhookEndpoint)
        assert endpoint.content.uuid == WEBHOOK_ENDPOINT_UUID

    def test_get_unknown_uuid_raises(self, bc, api):
        api.get(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            404, json=problem("not_found", 404)
        )

        with pytest.raises(NotFoundError):
            bc.webhook_endpoints.get(WEBHOOK_ENDPOINT_UUID)

    def test_filter_by_timeline(self, bc, api):
        route = api.get("/webhook_endpoints", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"webhook_endpoints": [webhook_endpoint_payload()], "next_cursor": None}
        )

        list(bc.webhook_endpoints.filter(timeline=TIMELINE_UUID))

        assert route.called

    def test_nested_iteration(self, bc, api, timeline):
        route = api.get("/webhook_endpoints", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"webhook_endpoints": [webhook_endpoint_payload()], "next_cursor": None}
        )

        list(timeline.webhook_endpoints)

        assert route.called


class TestEventsResource:
    def test_iteration_paginates(self, bc, api):
        api.get("/webhook_events").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "webhook_events": [webhook_event_payload()],
                        "next_cursor": "019e7750-66ee-7611-8e63-26d6c2a2c6f5",
                    },
                ),
                httpx.Response(
                    200, json={"webhook_events": [webhook_event_payload()], "next_cursor": None}
                ),
            ]
        )

        events = list(bc.webhook_events)

        assert len(events) == 2
        assert all(isinstance(e, WebhookEvent) for e in events)

    def test_get_typed_content(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload()}
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)

        assert event.content.content_type == "application/json"
        assert event.content.payload == '{"status":"ok"}'
        assert event.content.headers == {"HTTP_X_EXAMPLE_EVENT": "ping"}
        assert event.content.ingest_token_at_receipt == "019e7750-66ee-705a-803c-b25c5ee9b1f3"

    def test_event_carries_both_references(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload()}
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)

        assert event.webhook_endpoint.uuid == WEBHOOK_ENDPOINT_UUID
        assert event.timeline.uuid == TIMELINE_UUID

    def test_events_have_no_user(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload()}
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)

        with pytest.raises(AttributeError):
            event.user

    def test_get_not_a_viewer_raises(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            403, json=problem("not_a_viewer", 403)
        )

        with pytest.raises(ForbiddenError):
            bc.webhook_events.get(WEBHOOK_EVENT_UUID)

    def test_filter_by_endpoint_object(self, bc, api, endpoint):
        route = api.get("/webhook_events", params={"endpoint": WEBHOOK_ENDPOINT_UUID}).respond(
            200, json={"webhook_events": [webhook_event_payload()], "next_cursor": None}
        )

        list(bc.webhook_events.filter(endpoint=endpoint))

        assert route.called

    def test_filter_by_endpoint_uuid_string(self, bc, api):
        route = api.get("/webhook_events", params={"endpoint": WEBHOOK_ENDPOINT_UUID}).respond(
            200, json={"webhook_events": [], "next_cursor": None}
        )

        list(bc.webhook_events.filter(endpoint=WEBHOOK_ENDPOINT_UUID))

        assert route.called

    def test_timeline_and_endpoint_filters_compose(self, bc, api):
        route = api.get(
            "/webhook_events",
            params={"timeline": TIMELINE_UUID, "endpoint": WEBHOOK_ENDPOINT_UUID},
        ).respond(200, json={"webhook_events": [], "next_cursor": None})

        list(bc.webhook_events.filter(timeline=TIMELINE_UUID, endpoint=WEBHOOK_ENDPOINT_UUID))

        assert route.called

    def test_nested_iteration(self, bc, api, timeline):
        route = api.get("/webhook_events", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"webhook_events": [webhook_event_payload()], "next_cursor": None}
        )

        events = list(timeline.webhook_events)

        assert route.called
        assert all(isinstance(e, WebhookEvent) for e in events)
