"""Every resource, async: iteration, creation, filters, and awaited model verbs."""

import io
import json
from datetime import datetime, timezone

import httpx
import pytest

from basecradle import (
    Asset,
    Message,
    Session,
    Task,
    Timeline,
    TimelineLockedError,
    User,
    WebhookEndpoint,
    WebhookEvent,
)
from tests.conftest import (
    JOHN,
    NOVA,
    TIMELINE_UUID,
    WEBHOOK_ENDPOINT_UUID,
    asset_payload,
    directory_user_payload,
    message_payload,
    problem,
    session_payload,
    task_payload,
    timeline_payload,
    webhook_endpoint_payload,
    webhook_event_payload,
)

pytestmark = pytest.mark.anyio


async def alist(aiterable):
    """list(), for async iterables."""
    return [item async for item in aiterable]


@pytest.fixture
async def timeline(abc, api):
    api.get(f"/timelines/{TIMELINE_UUID}").respond(
        200, json={"timeline": timeline_payload(), "items": []}
    )
    return await abc.timelines.get(TIMELINE_UUID)


class TestAsyncTimelines:
    async def test_iteration_paginates(self, abc, api):
        api.get("/timelines").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"timelines": [timeline_payload()], "next_cursor": TIMELINE_UUID},
                ),
                httpx.Response(200, json={"timelines": [timeline_payload()], "next_cursor": None}),
            ]
        )

        timelines = await alist(abc.timelines)

        assert len(timelines) == 2
        assert all(isinstance(t, Timeline) for t in timelines)
        assert all(isinstance(t.owner, User) for t in timelines)

    async def test_pagination_is_lazy(self, abc, api):
        route = api.get("/timelines").mock(
            side_effect=[
                httpx.Response(
                    200, json={"timelines": [timeline_payload()], "next_cursor": TIMELINE_UUID}
                ),
                httpx.Response(200, json={"timelines": [timeline_payload()], "next_cursor": None}),
            ]
        )

        iterator = abc.timelines.__aiter__()
        await iterator.__anext__()  # first page's only item

        assert route.call_count == 1  # page 2 not fetched yet
        await alist(iterator)  # drain
        assert route.call_count == 2

    async def test_create(self, abc, api):
        route = api.post("/timelines").respond(
            201, json={"timeline": timeline_payload(participants=[]), "items": []}
        )

        timeline = await abc.timelines.create(name="Incident response")

        assert json.loads(route.calls.last.request.read()) == {
            "timeline": {"name": "Incident response"}
        }
        assert isinstance(timeline, Timeline)
        assert timeline.items == []

    async def test_get_merges_envelope(self, abc, api, timeline):
        assert timeline.name == "Incident response"
        assert timeline.items == []

    async def test_lock_is_awaited_and_updates_live_object(self, abc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/lock").respond(
            200, json={"uuid": TIMELINE_UUID, "locked": True}
        )

        assert timeline.locked is False
        await timeline.lock()

        assert timeline.locked is True

    async def test_add_participant_awaited(self, abc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/participations").respond(201, json=NOVA)

        added = await timeline.add_participant(NOVA["uuid"])

        assert json.loads(route.calls.last.request.read()) == {"user_id": NOVA["uuid"]}
        assert isinstance(added, User)

    async def test_remove_participant_awaited(self, abc, api, timeline):
        route = api.delete(f"/timelines/{TIMELINE_UUID}/participations/{NOVA['uuid']}").respond(204)

        await timeline.remove_participant(NOVA["uuid"])

        assert route.called
        assert timeline.participants == []


class TestAsyncItems:
    async def test_messages_iteration(self, abc, api):
        api.get("/messages").respond(
            200, json={"messages": [message_payload()], "next_cursor": None}
        )

        messages = await alist(abc.messages)

        assert all(isinstance(m, Message) for m in messages)
        assert messages[0].content.body == "Hello from a peer."

    async def test_filter_sends_params(self, abc, api):
        route = api.get("/messages", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"messages": [message_payload()], "next_cursor": None}
        )

        await alist(abc.messages.filter(timeline=TIMELINE_UUID))

        assert route.called

    async def test_task_filters_compose(self, abc, api):
        route = api.get("/tasks", params={"timeline": TIMELINE_UUID, "status": "pending"}).respond(
            200, json={"tasks": [task_payload()], "next_cursor": None}
        )

        await alist(abc.tasks.filter(timeline=TIMELINE_UUID, status="pending"))

        assert route.called

    async def test_get(self, abc, api):
        api.get(f"/assets/{asset_payload()['content']['uuid']}").respond(
            200, json={"asset": asset_payload()}
        )

        asset = await abc.assets.get(asset_payload()["content"]["uuid"])

        assert isinstance(asset, Asset)
        assert asset.content.file.filename == "report.pdf"

    async def test_nested_message_create(self, abc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/messages").respond(
            201, json={"message": message_payload()}
        )

        message = await timeline.messages.create(body="Hello from a peer.")

        assert json.loads(route.calls.last.request.read()) == {
            "message": {"body": "Hello from a peer."}
        }
        assert isinstance(message, Message)

    async def test_nested_asset_upload_multipart(self, abc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/assets").respond(
            201, json={"asset": asset_payload()}
        )
        fileobj = io.BytesIO(b"async upload bytes")
        fileobj.name = "report.pdf"

        asset = await timeline.assets.create(file=fileobj, description="Quarterly report")

        body = route.calls.last.request.read()
        assert b'name="asset[file]"' in body
        assert b"async upload bytes" in body
        assert isinstance(asset, Asset)

    async def test_nested_task_create_with_datetime(self, abc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/tasks").respond(
            201, json={"task": task_payload()}
        )

        task = await timeline.tasks.create(
            instructions="Summarize the thread",
            activate_at=datetime(2026, 1, 3, 15, 0, tzinfo=timezone.utc),
        )

        sent = json.loads(route.calls.last.request.read())
        assert sent["task"]["activate_at"] == "2026-01-03T15:00:00+00:00"
        assert isinstance(task, Task)

    async def test_nested_iteration(self, abc, api, timeline):
        route = api.get("/messages", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"messages": [message_payload()], "next_cursor": None}
        )

        messages = await alist(timeline.messages)

        assert route.called
        assert all(isinstance(m, Message) for m in messages)

    async def test_locked_timeline_raises_through_async(self, abc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/messages").respond(
            403, json=problem("timeline_locked", 403)
        )

        with pytest.raises(TimelineLockedError):
            await timeline.messages.create(body="Anyone there?")


class TestAsyncWebhooks:
    @pytest.fixture
    async def endpoint(self, abc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )
        return await timeline.webhook_endpoints.create(description="CI deploys")

    async def test_endpoint_lifecycle_awaited(self, abc, api, endpoint):
        api.delete(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/enablement").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload(enabled=False)}
        )
        api.post(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/enablement").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload(enabled=True)}
        )
        api.post(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/rotation").respond(
            200,
            json={
                "webhook_endpoint": webhook_endpoint_payload(
                    ingest_url="https://basecradle.com/webhooks/rotated"
                )
            },
        )

        await endpoint.disable()
        assert endpoint.content.enabled is False
        await endpoint.enable()
        assert endpoint.content.enabled is True
        await endpoint.rotate()
        assert endpoint.content.ingest_url == "https://basecradle.com/webhooks/rotated"

    async def test_events_iteration_and_filter(self, abc, api):
        route = api.get("/webhook_events", params={"endpoint": WEBHOOK_ENDPOINT_UUID}).respond(
            200, json={"webhook_events": [webhook_event_payload()], "next_cursor": None}
        )

        events = await alist(abc.webhook_events.filter(endpoint=WEBHOOK_ENDPOINT_UUID))

        assert route.called
        assert all(isinstance(e, WebhookEvent) for e in events)


class TestAsyncSessions:
    async def test_iteration(self, abc, api):
        api.get("/users/sessions").respond(
            200, json={"sessions": [session_payload()], "next_cursor": None}
        )

        sessions = await alist(abc.sessions)

        assert all(isinstance(s, Session) for s in sessions)

    async def test_revoke_awaited(self, abc, api):
        api.get("/users/sessions").respond(
            200, json={"sessions": [session_payload(current=False)], "next_cursor": None}
        )
        route = api.delete(f"/users/sessions/{session_payload()['uuid']}").respond(204)

        (session,) = await alist(abc.sessions)
        await session.revoke()

        assert route.called

    async def test_revoke_all_awaited(self, abc, api):
        route = api.delete("/users/sessions").respond(204)

        await abc.sessions.revoke_all()

        assert route.called


class TestAsyncUsers:
    async def test_directory_iteration(self, abc, api):
        api.get("/users").respond(200, json={"users": [directory_user_payload(user=NOVA)]})

        users = await alist(abc.users)

        assert all(isinstance(u, User) for u in users)
        assert users[0].handle == "nova"

    async def test_get(self, abc, api):
        api.get(f"/users/{JOHN['uuid']}").respond(
            200, json={"user": directory_user_payload(user=JOHN)}
        )

        john = await abc.users.get(JOHN["uuid"])

        assert isinstance(john, User)
        assert john.handle == "john"

    async def test_grant_trust_awaited_updates_live_object(self, abc, api):
        api.get("/users").respond(200, json={"users": [directory_user_payload(user=NOVA)]})
        api.post(f"/users/{NOVA['uuid']}/trust").respond(
            201, json={"user": directory_user_payload(user=NOVA, you_trust=True)}
        )

        (nova,) = await alist(abc.users)
        assert nova.trust.you_trust is False

        await nova.grant_trust()

        assert nova.trust.you_trust is True

    async def test_revoke_trust_awaited(self, abc, api):
        api.get("/users").respond(
            200, json={"users": [directory_user_payload(user=NOVA, you_trust=True)]}
        )
        api.delete(f"/users/{NOVA['uuid']}/trust").respond(204)

        (nova,) = await alist(abc.users)
        await nova.revoke_trust()

        assert nova.trust.you_trust is False


class TestAsyncWebhookEndpointsResource:
    async def test_top_level_iteration_and_get(self, abc, api):
        api.get("/webhook_endpoints").respond(
            200, json={"webhook_endpoints": [webhook_endpoint_payload()], "next_cursor": None}
        )
        api.get(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoints = await alist(abc.webhook_endpoints)
        fetched = await abc.webhook_endpoints.get(WEBHOOK_ENDPOINT_UUID)

        assert all(isinstance(e, WebhookEndpoint) for e in endpoints)
        assert fetched.content.uuid == WEBHOOK_ENDPOINT_UUID
