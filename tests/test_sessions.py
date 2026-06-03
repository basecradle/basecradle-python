"""Sessions: a peer managing its own credentials — list, revoke, revoke-all."""

import httpx
import pytest

from basecradle import NotFoundError, Session, SessionsResource, UnauthorizedError
from tests.conftest import API_SESSION_UUID, WEB_SESSION_UUID, problem, session_payload


class TestListing:
    def test_iteration_paginates(self, bc, api):
        api.get("/users/sessions").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "sessions": [session_payload()],
                        "next_cursor": API_SESSION_UUID,
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "sessions": [
                            session_payload(
                                uuid=WEB_SESSION_UUID, name=None, kind="web", current=False
                            )
                        ],
                        "next_cursor": None,
                    },
                ),
            ]
        )

        sessions = list(bc.sessions)

        assert len(sessions) == 2
        assert all(isinstance(s, Session) for s in sessions)

    def test_fields_are_wire_exact(self, bc, api):
        api.get("/users/sessions").respond(
            200,
            json={
                "sessions": [
                    session_payload(),
                    session_payload(uuid=WEB_SESSION_UUID, name=None, kind="web", current=False),
                ],
                "next_cursor": None,
            },
        )

        current, web = bc.sessions

        assert current.kind == "api"
        assert current.current is True
        assert current.name == "production agent"
        assert current.last_used_at == "2026-01-02T12:00:00.000Z"
        assert web.kind == "web"
        assert web.current is False
        assert web.name is None  # null name: never labeled

    def test_never_used_session_has_null_last_used_at(self, bc, api):
        api.get("/users/sessions").respond(
            200,
            json={"sessions": [session_payload(last_used_at=None)], "next_cursor": None},
        )

        (session,) = bc.sessions

        assert session.last_used_at is None

    def test_exactly_one_current_session(self, bc, api):
        """The documented invariant: current is True on exactly one row."""
        api.get("/users/sessions").respond(
            200,
            json={
                "sessions": [
                    session_payload(current=True),
                    session_payload(uuid=WEB_SESSION_UUID, kind="web", current=False),
                ],
                "next_cursor": None,
            },
        )

        currents = [s for s in bc.sessions if s.current]

        assert len(currents) == 1


class TestRevoke:
    def test_revoke_deletes_the_session(self, bc, api):
        api.get("/users/sessions").respond(
            200, json={"sessions": [session_payload(current=False)], "next_cursor": None}
        )
        route = api.delete(f"/users/sessions/{API_SESSION_UUID}").respond(204)

        (session,) = bc.sessions
        result = session.revoke()

        assert route.called
        assert result is None

    def test_revoking_someone_elses_session_raises_not_found(self, bc, api):
        """Other users' sessions are invisible — from your point of view they don't exist."""
        api.get("/users/sessions").respond(
            200, json={"sessions": [session_payload(current=False)], "next_cursor": None}
        )
        api.delete(f"/users/sessions/{API_SESSION_UUID}").respond(
            404, json=problem("not_found", 404)
        )

        (session,) = bc.sessions
        with pytest.raises(NotFoundError):
            session.revoke()

    def test_revoking_the_current_session_is_allowed(self, bc, api):
        """Self-rotation: the API allows it, and the SDK must not block it."""
        api.get("/users/sessions").respond(
            200, json={"sessions": [session_payload(current=True)], "next_cursor": None}
        )
        route = api.delete(f"/users/sessions/{API_SESSION_UUID}").respond(204)

        (session,) = bc.sessions
        assert session.current is True
        session.revoke()  # no exception, no nannying

        assert route.called


class TestRevokeAll:
    def test_revoke_all_deletes_the_collection(self, bc, api):
        route = api.delete("/users/sessions").respond(204)

        result = bc.sessions.revoke_all()

        assert route.called
        assert result is None

    def test_after_revoke_all_this_client_is_dead(self, bc, api):
        """The documented semantic, end-to-end: revoke_all kills the calling token too."""
        api.delete("/users/sessions").respond(204)
        api.get("/users/dashboard").respond(
            401,
            json=problem(
                "unauthorized", 401, detail="Authentication is required to access this resource."
            ),
        )

        bc.sessions.revoke_all()

        with pytest.raises(UnauthorizedError):
            bc.me  # the very next call on this client fails


class TestDocumentation:
    """The dangerous semantics must be documented where a consumer will see them."""

    def test_revoke_all_docstring_warns_about_the_calling_token(self):
        docstring = SessionsResource.revoke_all.__doc__
        assert "the token this client is using" in docstring
        assert "AuthenticationError" in docstring
        assert "login" in docstring.lower()  # tells you how to recover

    def test_revoke_docstring_warns_about_current_session(self):
        docstring = Session.revoke.__doc__
        assert "current" in docstring.lower()
        assert "self-rotation" in docstring.lower()

    def test_readme_documents_both_sharp_edges(self):
        from pathlib import Path

        readme = (Path(__file__).parent.parent / "README.md").read_text()
        assert "revoke_all" in readme
        assert "self-rotation" in readme
