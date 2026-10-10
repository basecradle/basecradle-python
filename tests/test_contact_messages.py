"""Contact messages and notes — the admin-only surface, on both clients."""

import json

import httpx
import pytest

from basecradle import (
    ContactMessage,
    ForbiddenError,
    InvalidFilterError,
    NotAnAdminError,
    Note,
    NotFoundError,
    RequestHeaders,
    User,
    ValidationError,
)
from tests.conftest import (
    CONTACT_MESSAGE_UUID,
    JOHN,
    NOTE_UUID,
    NOVA,
    contact_message_payload,
    note_payload,
    problem,
)

SECOND_CONTACT_MESSAGE_UUID = "01a12422-0110-72fb-aef4-cc0df5822aaa"
SECOND_NOTE_UUID = "01a12422-0110-78e5-843c-7e52cbf5140b"


def not_an_admin():
    return problem("not_an_admin", 403, title="Admin Action Required")


@pytest.fixture
def message(bc, api):
    api.get(f"/contact_messages/{CONTACT_MESSAGE_UUID}").respond(
        200, json={"contact_message": contact_message_payload()}
    )
    return bc.contact_messages.get(CONTACT_MESSAGE_UUID)


class TestListing:
    def test_iteration_paginates_newest_first(self, bc, api):
        route = api.get("/contact_messages").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "contact_messages": [contact_message_payload()],
                        "next_cursor": CONTACT_MESSAGE_UUID,
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "contact_messages": [
                            contact_message_payload(uuid=SECOND_CONTACT_MESSAGE_UUID)
                        ],
                        "next_cursor": None,
                    },
                ),
            ]
        )

        messages = list(bc.contact_messages)

        assert [m.uuid for m in messages] == [CONTACT_MESSAGE_UUID, SECOND_CONTACT_MESSAGE_UUID]
        assert all(isinstance(m, ContactMessage) for m in messages)
        assert route.calls[1].request.url.params["before"] == CONTACT_MESSAGE_UUID

    def test_filter_by_status(self, bc, api):
        route = api.get("/contact_messages", params={"status": "received"}).respond(
            200, json={"contact_messages": [contact_message_payload()], "next_cursor": None}
        )

        (only,) = bc.contact_messages.filter(status="received")

        assert only.status == "received"
        assert route.calls.last.request.url.params["status"] == "received"

    def test_filter_returns_a_new_lazy_resource(self, bc):
        """No request until iteration, and the unfiltered resource is untouched."""
        narrowed = bc.contact_messages.filter(status="spam")

        assert narrowed is not bc.contact_messages
        assert narrowed._filters == {"status": "spam"}
        assert bc.contact_messages._filters == {}
        assert bc.contact_messages.filter()._filters == {}

    def test_an_unknown_status_filter_raises_invalid_filter(self, bc, api):
        api.get("/contact_messages").respond(400, json=problem("invalid_filter", 400))

        with pytest.raises(InvalidFilterError):
            list(bc.contact_messages.filter(status="archived"))

    def test_repr_shows_the_path_and_filters(self, bc):
        assert repr(bc.contact_messages.filter(status="closed")) == (
            "<ContactMessagesResource path='/contact_messages' filters={'status': 'closed'}>"
        )


class TestTheRecord:
    def test_fields_are_wire_exact_and_flat(self, message):
        assert message.uuid == CONTACT_MESSAGE_UUID
        assert message.name == "Nova Digital"
        assert message.email_address == "nova@example.com"
        assert message.status == "received"
        assert message.ip_address == "203.0.113.42"
        assert message.honeypot_filled is False
        assert message.fill_seconds == 42
        assert message.created_at == "2026-10-10T03:09:05.000Z"
        with pytest.raises(AttributeError):
            message.content  # a top-level record: no type/content envelope

    def test_a_visitor_without_an_account_has_no_user(self, message):
        assert message.user is None

    def test_a_signed_in_sender_is_a_user(self, bc, api):
        api.get(f"/contact_messages/{CONTACT_MESSAGE_UUID}").respond(
            200, json={"contact_message": contact_message_payload(user=NOVA)}
        )

        message = bc.contact_messages.get(CONTACT_MESSAGE_UUID)

        assert isinstance(message.user, User)
        assert message.user.handle == "nova"

    def test_data_is_the_wire_dict_untouched(self, message):
        """One self-describing slot per vendor, each outcome handed back as sent."""
        assert isinstance(message.data, dict)
        assert message.data == contact_message_payload()["data"]
        assert message.data["proxycheck"]["answer"]["status"] == "ok"
        assert message.data["google"]["skipped"] == "no token"
        assert message.data["abuseipdb"]["error"]["class"] == "Timeout"

    def test_notes_are_embedded_in_full(self, message):
        (note,) = message.notes

        assert isinstance(note, Note)
        assert note == Note(note_payload())
        assert note.user.handle == "john"
        assert note.notable.type == "contact_message"
        assert note.notable.uuid == CONTACT_MESSAGE_UUID

    def test_headers_fold_case_and_print_names_only(self, message):
        headers = message.headers

        assert isinstance(headers, RequestHeaders)
        assert headers["accept-language"] == "en-US,en;q=0.9"
        assert headers == contact_message_payload()["headers"]  # wire-exact pairs
        assert repr(headers) == "<RequestHeaders ['Accept-Language', 'Host', 'User-Agent']>"
        assert "en-US" not in repr(headers)

    def test_a_header_recorded_without_a_value_reads_none(self, bc, api):
        """The spec allows ``null`` values here, so ``None`` is a value, not "not sent"."""
        api.get(f"/contact_messages/{CONTACT_MESSAGE_UUID}").respond(
            200,
            json={"contact_message": contact_message_payload(headers={"Host": None})},
        )

        headers = bc.contact_messages.get(CONTACT_MESSAGE_UUID).headers

        assert headers["host"] is None
        assert "Host" in headers
        assert "User-Agent" not in headers
        with pytest.raises(KeyError, match="No 'User-Agent' header on this request"):
            headers["User-Agent"]


class TestSetStatus:
    def test_set_status_patches_and_adopts_the_whole_record(self, bc, api, message):
        route = api.patch(f"/contact_messages/{CONTACT_MESSAGE_UUID}/status").respond(
            200,
            json={
                "contact_message": contact_message_payload(
                    status="closed", updated_at="2026-10-10T05:00:00.000Z"
                )
            },
        )

        result = message.set_status("closed")

        assert result is None
        assert json.loads(route.calls.last.request.content) == {
            "contact_message": {"status": "closed"}
        }
        assert message.status == "closed"
        assert message.updated_at == "2026-10-10T05:00:00.000Z"

    def test_an_unknown_status_raises_validation_error(self, bc, api, message):
        api.patch(f"/contact_messages/{CONTACT_MESSAGE_UUID}/status").respond(
            422, json=problem("validation_failed", 422, errors={"status": ["is not included"]})
        )

        with pytest.raises(ValidationError) as caught:
            message.set_status("archived")

        assert caught.value.errors == {"status": ["is not included"]}
        assert message.status == "received"  # nothing confirmed, nothing adopted


class TestAddNote:
    def test_add_note_posts_returns_and_appends(self, bc, api, message):
        added = note_payload(uuid=SECOND_NOTE_UUID, body="Marked as a real lead.", user=NOVA)
        route = api.post(f"/contact_messages/{CONTACT_MESSAGE_UUID}/notes").respond(
            201, json={"note": added}, headers={"Location": f"/notes/{SECOND_NOTE_UUID}"}
        )

        note = message.add_note(body="Marked as a real lead.")

        assert json.loads(route.calls.last.request.content) == {
            "note": {"body": "Marked as a real lead."}
        }
        assert isinstance(note, Note)
        assert note.uuid == SECOND_NOTE_UUID
        assert note.user.handle == "nova"
        assert [n.uuid for n in message.notes] == [NOTE_UUID, SECOND_NOTE_UUID]

    def test_a_blank_body_raises_validation_error_and_appends_nothing(self, bc, api, message):
        api.post(f"/contact_messages/{CONTACT_MESSAGE_UUID}/notes").respond(
            422, json=problem("validation_failed", 422, errors={"body": ["can't be blank"]})
        )

        with pytest.raises(ValidationError):
            message.add_note(body="")

        assert [n.uuid for n in message.notes] == [NOTE_UUID]

    def test_body_is_keyword_only(self, message):
        with pytest.raises(TypeError):
            message.add_note("positional")


class TestNotes:
    def test_iteration_paginates(self, bc, api):
        route = api.get("/notes").mock(
            side_effect=[
                httpx.Response(200, json={"notes": [note_payload()], "next_cursor": NOTE_UUID}),
                httpx.Response(
                    200,
                    json={"notes": [note_payload(uuid=SECOND_NOTE_UUID)], "next_cursor": None},
                ),
            ]
        )

        notes = list(bc.notes)

        assert [n.uuid for n in notes] == [NOTE_UUID, SECOND_NOTE_UUID]
        assert route.calls[1].request.url.params["before"] == NOTE_UUID

    def test_get(self, bc, api):
        api.get(f"/notes/{NOTE_UUID}").respond(200, json={"note": note_payload()})

        note = bc.notes.get(NOTE_UUID)

        assert note.body == "Looks genuine. Replied by email."
        assert note.user == User(JOHN)

    def test_an_unknown_note_raises_not_found(self, bc, api):
        api.get(f"/notes/{SECOND_NOTE_UUID}").respond(404, json=problem("not_found", 404))

        with pytest.raises(NotFoundError):
            bc.notes.get(SECOND_NOTE_UUID)


class TestAdminOnly:
    """Every operation answers 403 ``not_an_admin`` to anyone else — typed, and a 403."""

    @pytest.mark.parametrize(
        ("method", "path", "call"),
        [
            ("GET", "/contact_messages", lambda bc: list(bc.contact_messages)),
            (
                "GET",
                f"/contact_messages/{CONTACT_MESSAGE_UUID}",
                lambda bc: bc.contact_messages.get(CONTACT_MESSAGE_UUID),
            ),
            ("GET", "/notes", lambda bc: list(bc.notes)),
            ("GET", f"/notes/{NOTE_UUID}", lambda bc: bc.notes.get(NOTE_UUID)),
            (
                "PATCH",
                f"/contact_messages/{CONTACT_MESSAGE_UUID}/status",
                lambda bc: ContactMessage(contact_message_payload(), client=bc).set_status(
                    "closed"
                ),
            ),
            (
                "POST",
                f"/contact_messages/{CONTACT_MESSAGE_UUID}/notes",
                lambda bc: ContactMessage(contact_message_payload(), client=bc).add_note(
                    body="Hello"
                ),
            ),
        ],
    )
    def test_a_non_admin_gets_not_an_admin(self, bc, api, method, path, call):
        api.route(method=method, path=path).respond(403, json=not_an_admin())

        with pytest.raises(NotAnAdminError) as caught:
            call(bc)

        assert isinstance(caught.value, ForbiddenError)
        assert caught.value.code == "not_an_admin"
        assert caught.value.status == 403


class TestAsync:
    pytestmark = pytest.mark.anyio

    async def test_list_filter_get(self, abc, api):
        api.get("/contact_messages", params={"status": "spam"}).respond(
            200,
            json={
                "contact_messages": [contact_message_payload(status="spam")],
                "next_cursor": None,
            },
        )
        api.get(f"/contact_messages/{CONTACT_MESSAGE_UUID}").respond(
            200, json={"contact_message": contact_message_payload()}
        )

        listed = [m async for m in abc.contact_messages.filter(status="spam")]
        fetched = await abc.contact_messages.get(CONTACT_MESSAGE_UUID)

        assert [m.status for m in listed] == ["spam"]
        assert isinstance(fetched, ContactMessage)

    async def test_verbs_are_awaited(self, abc, api):
        api.get(f"/contact_messages/{CONTACT_MESSAGE_UUID}").respond(
            200, json={"contact_message": contact_message_payload()}
        )
        api.patch(f"/contact_messages/{CONTACT_MESSAGE_UUID}/status").respond(
            200, json={"contact_message": contact_message_payload(status="closed")}
        )
        api.post(f"/contact_messages/{CONTACT_MESSAGE_UUID}/notes").respond(
            201, json={"note": note_payload(uuid=SECOND_NOTE_UUID, body="Closed it.")}
        )
        message = await abc.contact_messages.get(CONTACT_MESSAGE_UUID)

        assert await message.set_status("closed") is None
        note = await message.add_note(body="Closed it.")

        assert message.status == "closed"
        assert note.body == "Closed it."
        assert [n.uuid for n in message.notes] == [NOTE_UUID, SECOND_NOTE_UUID]

    async def test_notes(self, abc, api):
        api.get("/notes").respond(200, json={"notes": [note_payload()], "next_cursor": None})
        api.get(f"/notes/{NOTE_UUID}").respond(200, json={"note": note_payload()})

        listed = [n async for n in abc.notes]
        fetched = await abc.notes.get(NOTE_UUID)

        assert listed == [fetched]
        assert isinstance(fetched, Note)

    async def test_not_an_admin(self, abc, api):
        api.get("/notes").respond(403, json=not_an_admin())

        with pytest.raises(NotAnAdminError):
            [n async for n in abc.notes]
