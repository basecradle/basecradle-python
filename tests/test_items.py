"""Timeline items — messages, assets, tasks: nested create, top-level list/get, .filter()."""

import io
import json
from datetime import datetime, timezone

import httpx
import pytest

from basecradle import (
    Asset,
    AssetFile,
    ForbiddenError,
    Message,
    NotFoundError,
    Task,
    TimelineLockedError,
    User,
    ValidationError,
)
from tests.conftest import (
    ASSET_UUID,
    MESSAGE_UUID,
    TASK_UUID,
    TIMELINE_UUID,
    asset_payload,
    message_payload,
    problem,
    task_payload,
    timeline_payload,
)

# (resource name on the client, path, envelope key, payload builder, model, content uuid)
RESOURCES = [
    ("messages", "/messages", "messages", message_payload, Message, MESSAGE_UUID),
    ("assets", "/assets", "assets", asset_payload, Asset, ASSET_UUID),
    ("tasks", "/tasks", "tasks", task_payload, Task, TASK_UUID),
]


@pytest.fixture
def timeline(bc, api):
    api.get(f"/timelines/{TIMELINE_UUID}").respond(
        200, json={"timeline": timeline_payload(), "items": []}
    )
    return bc.timelines.get(TIMELINE_UUID)


@pytest.mark.parametrize(("name", "path", "envelope", "payload", "model", "uuid"), RESOURCES)
class TestOnePatternThreeResources:
    """The shared list/get/filter pattern, pinned identically for all three resources."""

    def test_iteration_paginates(self, bc, api, name, path, envelope, payload, model, uuid):
        api.get(path).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        envelope: [payload()],
                        "next_cursor": "019e7750-66ee-7611-8e63-26d6c2a2c6f5",
                    },
                ),
                httpx.Response(200, json={envelope: [payload()], "next_cursor": None}),
            ]
        )

        items = list(getattr(bc, name))

        assert len(items) == 2
        assert all(isinstance(item, model) for item in items)
        assert all(isinstance(item.user, User) for item in items)

    def test_get_returns_typed_model(self, bc, api, name, path, envelope, payload, model, uuid):
        singular = envelope[:-1]  # messages → message, assets → asset, tasks → task
        api.get(f"{path}/{uuid}").respond(200, json={singular: payload()})

        item = getattr(bc, name).get(uuid)

        assert isinstance(item, model)
        assert item.content.uuid == uuid
        assert item.timeline.uuid == TIMELINE_UUID  # reference form: just the uuid

    def test_get_not_a_viewer_raises(self, bc, api, name, path, envelope, payload, model, uuid):
        api.get(f"{path}/{uuid}").respond(403, json=problem("not_a_viewer", 403))

        with pytest.raises(ForbiddenError):
            getattr(bc, name).get(uuid)

    def test_get_unknown_uuid_raises(self, bc, api, name, path, envelope, payload, model, uuid):
        api.get(f"{path}/{uuid}").respond(404, json=problem("not_found", 404))

        with pytest.raises(NotFoundError):
            getattr(bc, name).get(uuid)

    def test_filter_by_timeline_object(self, bc, api, name, path, envelope, payload, model, uuid):
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200, json={"timeline": timeline_payload(), "items": []}
        )
        route = api.get(path, params={"timeline": TIMELINE_UUID}).respond(
            200, json={envelope: [payload()], "next_cursor": None}
        )

        timeline = bc.timelines.get(TIMELINE_UUID)
        list(getattr(bc, name).filter(timeline=timeline))

        assert route.called

    def test_filter_by_uuid_string(self, bc, api, name, path, envelope, payload, model, uuid):
        route = api.get(path, params={"timeline": TIMELINE_UUID}).respond(
            200, json={envelope: [payload()], "next_cursor": None}
        )

        list(getattr(bc, name).filter(timeline=TIMELINE_UUID))

        assert route.called

    def test_filter_does_not_mutate_the_original_resource(
        self, bc, api, name, path, envelope, payload, model, uuid
    ):
        resource = getattr(bc, name)
        filtered = resource.filter(timeline=TIMELINE_UUID)

        assert filtered is not resource
        assert resource._filters == {}


class TestContentTyping:
    def test_message_content(self, bc, api):
        api.get(f"/messages/{MESSAGE_UUID}").respond(200, json={"message": message_payload()})

        message = bc.messages.get(MESSAGE_UUID)

        assert message.content.body == "Hello from a peer."

    def test_asset_content_and_file(self, bc, api):
        api.get(f"/assets/{ASSET_UUID}").respond(200, json={"asset": asset_payload()})

        asset = bc.assets.get(ASSET_UUID)

        assert asset.content.description == "Quarterly report"
        assert isinstance(asset.content.file, AssetFile)
        assert asset.content.file.filename == "report.pdf"
        assert asset.content.file.byte_size == 184320
        assert asset.content.file.url.startswith("https://basecradle.com/")

    def test_task_content(self, bc, api):
        api.get(f"/tasks/{TASK_UUID}").respond(200, json={"task": task_payload()})

        task = bc.tasks.get(TASK_UUID)

        assert task.content.instructions == "Summarize the thread"
        assert task.content.activate_at == "2026-01-03T15:00:00.000Z"
        assert task.content.status == "pending"


class TestTaskFilters:
    def test_status_filter(self, bc, api):
        route = api.get("/tasks", params={"status": "pending"}).respond(
            200, json={"tasks": [task_payload()], "next_cursor": None}
        )

        list(bc.tasks.filter(status="pending"))

        assert route.called

    def test_timeline_and_status_compose(self, bc, api):
        route = api.get(
            "/tasks", params={"timeline": TIMELINE_UUID, "status": "activated"}
        ).respond(200, json={"tasks": [], "next_cursor": None})

        list(bc.tasks.filter(timeline=TIMELINE_UUID, status="activated"))

        assert route.called

    def test_filters_chain(self, bc, api):
        route = api.get("/tasks", params={"timeline": TIMELINE_UUID, "status": "pending"}).respond(
            200, json={"tasks": [], "next_cursor": None}
        )

        list(bc.tasks.filter(timeline=TIMELINE_UUID).filter(status="pending"))

        assert route.called


class TestCreateMessage:
    def test_create_sends_rails_nested_body(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/messages").respond(
            201, json={"message": message_payload()}
        )

        message = timeline.messages.create(body="Hello from a peer.")

        assert json.loads(route.calls.last.request.read()) == {
            "message": {"body": "Hello from a peer."}
        }
        assert isinstance(message, Message)
        assert message.content.body == "Hello from a peer."

    def test_create_on_locked_timeline_raises_timeline_locked(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/messages").respond(
            403, json=problem("timeline_locked", 403, detail="The timeline is locked.")
        )

        with pytest.raises(TimelineLockedError):
            timeline.messages.create(body="Anyone there?")

        # TimelineLockedError is a ForbiddenError — both catches work.
        api.post(f"/timelines/{TIMELINE_UUID}/messages").respond(
            403, json=problem("timeline_locked", 403)
        )
        with pytest.raises(ForbiddenError):
            timeline.messages.create(body="Anyone there?")

    def test_create_blank_body_raises_validation_error(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/messages").respond(
            422,
            json=problem("validation_failed", 422, errors={"body": ["can't be blank"]}),
        )

        with pytest.raises(ValidationError) as exc_info:
            timeline.messages.create(body="")

        assert exc_info.value.errors == {"body": ["can't be blank"]}


class TestCreateAsset:
    def test_upload_from_path_sends_multipart(self, bc, api, timeline, tmp_path):
        report = tmp_path / "report.pdf"
        report.write_bytes(b"%PDF-1.7 fabricated test bytes")
        route = api.post(f"/timelines/{TIMELINE_UUID}/assets").respond(
            201, json={"asset": asset_payload()}
        )

        asset = timeline.assets.create(file=str(report), description="Quarterly report")

        request = route.calls.last.request
        assert request.headers["Content-Type"].startswith("multipart/form-data")
        body = request.read()
        assert b'name="asset[file]"' in body
        assert b'filename="report.pdf"' in body
        assert b"%PDF-1.7 fabricated test bytes" in body
        assert b'name="asset[description]"' in body
        assert b"Quarterly report" in body
        assert isinstance(asset, Asset)
        assert asset.content.file.url

    def test_upload_from_file_object(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/assets").respond(
            201, json={"asset": asset_payload()}
        )
        fileobj = io.BytesIO(b"in-memory bytes")
        fileobj.name = "notes.txt"

        timeline.assets.create(file=fileobj, description="Notes")

        body = route.calls.last.request.read()
        assert b'filename="notes.txt"' in body
        assert b"in-memory bytes" in body

    def test_upload_without_description(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/assets").respond(
            201, json={"asset": asset_payload()}
        )

        timeline.assets.create(file=io.BytesIO(b"bytes"))

        body = route.calls.last.request.read()
        assert b'name="asset[file]"' in body
        assert b'name="asset[description]"' not in body

    def test_upload_to_locked_timeline_raises(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/assets").respond(
            403, json=problem("timeline_locked", 403)
        )

        with pytest.raises(TimelineLockedError):
            timeline.assets.create(file=io.BytesIO(b"bytes"))

    def test_missing_file_raises_validation_error(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/assets").respond(
            422, json=problem("validation_failed", 422, errors={"file": ["can't be blank"]})
        )

        with pytest.raises(ValidationError):
            timeline.assets.create(file=io.BytesIO(b""))


class TestCreateTask:
    def test_create_with_aware_datetime(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/tasks").respond(
            201, json={"task": task_payload()}
        )

        task = timeline.tasks.create(
            instructions="Summarize the thread",
            activate_at=datetime(2026, 1, 3, 15, 0, tzinfo=timezone.utc),
        )

        sent = json.loads(route.calls.last.request.read())
        assert sent == {
            "task": {
                "instructions": "Summarize the thread",
                "activate_at": "2026-01-03T15:00:00+00:00",
            }
        }
        assert isinstance(task, Task)
        assert task.content.status == "pending"

    def test_create_with_naive_datetime(self, bc, api, timeline):
        """Naive datetimes serialize without an offset — the API interprets them in the
        account's time zone (documented behavior)."""
        route = api.post(f"/timelines/{TIMELINE_UUID}/tasks").respond(
            201, json={"task": task_payload()}
        )

        timeline.tasks.create(instructions="Review", activate_at=datetime(2026, 1, 3, 15, 0))

        sent = json.loads(route.calls.last.request.read())
        assert sent["task"]["activate_at"] == "2026-01-03T15:00:00"

    def test_create_with_iso_string(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/tasks").respond(
            201, json={"task": task_payload()}
        )

        timeline.tasks.create(instructions="Review", activate_at="2026-01-03T15:00:00Z")

        sent = json.loads(route.calls.last.request.read())
        assert sent["task"]["activate_at"] == "2026-01-03T15:00:00Z"

    def test_create_on_locked_timeline_raises(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/tasks").respond(
            403, json=problem("timeline_locked", 403)
        )

        with pytest.raises(TimelineLockedError):
            timeline.tasks.create(instructions="Review", activate_at="2026-01-03T15:00:00Z")


class TestNestedIteration:
    """timeline.messages / .assets / .tasks iterate that timeline's items via the filter."""

    def test_timeline_messages_iterates_filtered(self, bc, api, timeline):
        route = api.get("/messages", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"messages": [message_payload()], "next_cursor": None}
        )

        messages = list(timeline.messages)

        assert route.called
        assert all(isinstance(m, Message) for m in messages)

    def test_timeline_assets_iterates_filtered(self, bc, api, timeline):
        route = api.get("/assets", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"assets": [asset_payload()], "next_cursor": None}
        )

        list(timeline.assets)

        assert route.called

    def test_timeline_tasks_iterates_filtered(self, bc, api, timeline):
        route = api.get("/tasks", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"tasks": [task_payload()], "next_cursor": None}
        )

        list(timeline.tasks)

        assert route.called
