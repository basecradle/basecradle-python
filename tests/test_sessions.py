"""A peer managing its own credentials: list, revoke, revoke-all, sign out, change password."""

import json as jsonlib

import httpx
import pytest

from basecradle import (
    CurrentPasswordIncorrectError,
    NotFoundError,
    PasswordConfirmationMismatchError,
    Session,
    SessionsResource,
    UnauthorizedError,
    ValidationError,
)
from tests.conftest import (
    API_SESSION_UUID,
    DASHBOARD_RESPONSE,
    WEB_SESSION_UUID,
    problem,
    session_payload,
)

CURRENT = "correct-horse-battery-staple"
NEW = "Tr0ub4dor&3-new"


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


class TestSignOut:
    def test_sign_out_deletes_the_current_session(self, bc, api):
        route = api.delete("/session").respond(204)

        result = bc.sign_out()

        assert route.called
        assert result is None

    def test_after_sign_out_this_client_is_dead(self, bc, api):
        """Signing out kills the calling token: the very next call on this client fails."""
        api.delete("/session").respond(204)
        api.get("/users/dashboard").respond(
            401,
            json=problem(
                "unauthorized", 401, detail="Authentication is required to access this resource."
            ),
        )

        bc.sign_out()

        with pytest.raises(UnauthorizedError):
            bc.me


class TestChangePassword:
    """``bc.change_password()`` — the last self-credential a peer could not touch typed."""

    def test_sends_all_three_fields_and_returns_none(self, bc, api):
        route = api.patch("/users/password").respond(204)

        result = bc.change_password(
            current_password=CURRENT, password=NEW, password_confirmation=NEW
        )

        assert result is None
        assert jsonlib.loads(route.calls.last.request.read()) == {
            "current_password": CURRENT,
            "password": NEW,
            "password_confirmation": NEW,
        }

    def test_confirmation_defaults_to_the_new_password(self, bc, api):
        """The API requires the field; handing one string to two arguments is not a check."""
        route = api.patch("/users/password").respond(204)

        bc.change_password(current_password=CURRENT, password=NEW)

        assert jsonlib.loads(route.calls.last.request.read())["password_confirmation"] == NEW

    def test_an_explicit_confirmation_is_sent_verbatim(self, bc, api):
        """A caller with a real second entry keeps it — the platform, not the SDK, judges it."""
        route = api.patch("/users/password").respond(
            422, json=problem("password_confirmation_mismatch", 422)
        )

        with pytest.raises(PasswordConfirmationMismatchError):
            bc.change_password(
                current_password=CURRENT, password=NEW, password_confirmation="mistyped"
            )

        assert jsonlib.loads(route.calls.last.request.read())["password_confirmation"] == "mistyped"

    def test_wrong_current_password_raises_typed(self, bc, api):
        api.patch("/users/password").respond(
            422,
            json=problem(
                "current_password_incorrect", 422, detail="The current password is incorrect."
            ),
        )

        with pytest.raises(CurrentPasswordIncorrectError) as caught:
            bc.change_password(current_password="wrong", password=NEW)

        assert caught.value.code == "current_password_incorrect"
        assert isinstance(caught.value, ValidationError)

    def test_a_weak_new_password_raises_validation_error(self, bc, api):
        """Strength rules are the platform's; the SDK sends what it is given."""
        api.patch("/users/password").respond(
            422, json=problem("validation_failed", 422, errors={"password": ["is too short"]})
        )

        with pytest.raises(ValidationError):
            bc.change_password(current_password=CURRENT, password="short")

    def test_arguments_are_keyword_only(self, bc, api):
        """Three interchangeable strings must never be positional — a swap is silent.

        ``api`` is requested even though nothing should be sent: if the guard regresses,
        the call falls through to a real ``PATCH`` and respx must be there to catch it
        rather than letting the suite reach basecradle.com.
        """
        with pytest.raises(TypeError):
            bc.change_password(CURRENT, NEW)

    def test_an_explicit_none_confirmation_is_refused(self, bc, api):
        """``password_confirmation=form.get("confirm")`` with no box filled is a caller bug.

        Falling back to ``password`` here would change the password with no confirmation at
        all — precisely what a caller passing the argument was trying to guard against.
        """
        with pytest.raises(TypeError, match="Omit it entirely"):
            bc.change_password(current_password=CURRENT, password=NEW, password_confirmation=None)

    def test_the_verb_leaves_this_client_untouched(self, bc, api):
        """Changing a password signs nothing out, so the SDK must not tear the client down.

        The platform's half of that ("every session stays valid") is its own; what is
        *this* SDK's to keep is the client — the token it holds, and the credential
        ``login()`` recorded. A later "helpful" edit that signed out or cleared the token
        after a password change would break the documented semantics, and fail here.
        """
        api.patch("/users/password").respond(204)
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)
        token_before, session_before = bc.token, bc.session

        bc.change_password(current_password=CURRENT, password=NEW)
        next_call = bc.me

        assert bc.token == token_before
        assert bc.session is session_before
        assert next_call.identity.handle  # the same client still works, same credential
        assert api.calls.last.request.headers["Authorization"] == f"Bearer {token_before}"


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

    def test_sign_out_docstring_warns_the_client_dies(self):
        from basecradle import BaseCradle

        docstring = BaseCradle.sign_out.__doc__
        assert "AuthenticationError" in docstring
        assert "current" in docstring.lower()  # equals revoking your current session

    def test_change_password_docstrings_say_it_signs_nothing_out(self):
        """Both clients, or an async reader gets a stale claim from ``help()``."""
        from basecradle import AsyncBaseCradle, BaseCradle

        for cls in (BaseCradle, AsyncBaseCradle):
            docstring = cls.change_password.__doc__
            assert "signs nothing out" in docstring, f"{cls.__name__} lost the warning"
            assert "revoke" in docstring  # points at what remediation actually is

    def test_readme_documents_both_sharp_edges(self):
        from pathlib import Path

        readme = (Path(__file__).parent.parent / "README.md").read_text()
        assert "revoke_all" in readme
        assert "self-rotation" in readme
