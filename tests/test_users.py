"""Users & trust: the directory, access-tiered profiles, and the trust handshake."""

import pytest

from basecradle import NotFoundError, User
from tests.conftest import (
    DASHBOARD_RESPONSE,
    JOHN,
    NOVA,
    TIMELINE_UUID,
    directory_user_payload,
    problem,
    timeline_payload,
    trusted_peer_user_payload,
)


class TestDirectory:
    def test_iteration_returns_users(self, bc, api):
        api.get("/users").respond(
            200,
            json={
                "users": [
                    directory_user_payload(user=NOVA),
                    directory_user_payload(user=JOHN, you_trust=True, trusts_you=True),
                ]
            },
        )

        users = list(bc.users)

        assert len(users) == 2
        assert all(isinstance(u, User) for u in users)
        assert [u.handle for u in users] == ["nova", "john"]

    def test_directory_is_a_single_unpaginated_request(self, bc, api):
        """The directory has no cursor — one request, no pagination params."""
        route = api.get("/users").respond(200, json={"users": [directory_user_payload()]})

        list(bc.users)

        assert route.call_count == 1
        assert "before" not in str(route.calls.last.request.url)

    def test_rows_are_lean_but_trust_is_always_readable(self, bc, api):
        api.get("/users").respond(
            200, json={"users": [directory_user_payload(you_trust=True, trusts_you=True)]}
        )

        (nova,) = bc.users

        assert nova.trust.you_trust is True
        assert nova.trust.trusts_you is True
        assert nova.trust.mutual is True
        # Lean row: the access-gated clusters are not there.
        assert not hasattr(nova, "about")

    def test_empty_directory(self, bc, api):
        api.get("/users").respond(200, json={"users": []})

        assert list(bc.users) == []


class TestGet:
    def test_untrusted_view_is_lean(self, bc, api):
        api.get(f"/users/{NOVA['uuid']}").respond(
            200, json={"user": directory_user_payload(user=NOVA)}
        )

        nova = bc.users.get(NOVA["uuid"])

        assert nova.handle == "nova"
        assert nova.kind == "ai"
        assert not hasattr(nova, "suspended")

    def test_trusted_peer_view_adds_the_middle_tier(self, bc, api):
        api.get(f"/users/{JOHN['uuid']}").respond(
            200, json={"user": trusted_peer_user_payload(user=JOHN, trusts_you=True)}
        )

        john = bc.users.get(JOHN["uuid"])

        assert john.about == "Building things at BaseCradle."
        assert john.max_timelines == 15
        # Still not the self/admin cluster.
        assert not hasattr(john, "integration_url")

    def test_self_view_has_everything(self, bc, api):
        api.get(f"/users/{NOVA['uuid']}").respond(200, json={"user": DASHBOARD_RESPONSE["you"]})

        me = bc.users.get(NOVA["uuid"])

        assert me.visible is True
        assert me.integration_enabled is False
        assert me.created_at == "2026-01-01T00:00:00.000Z"

    def test_unknown_or_hidden_user_raises_not_found(self, bc, api):
        api.get(f"/users/{NOVA['uuid']}").respond(404, json=problem("not_found", 404))

        with pytest.raises(NotFoundError):
            bc.users.get(NOVA["uuid"])


class TestAccessTiers:
    """Acceptance criterion: tier-dependent fields handled gracefully and documented."""

    def test_absent_tier_field_raises_helpful_error_not_silent_none(self, bc, api):
        api.get(f"/users/{NOVA['uuid']}").respond(
            200, json={"user": directory_user_payload(user=NOVA)}
        )

        nova = bc.users.get(NOVA["uuid"])

        with pytest.raises(AttributeError) as exc_info:
            nova.integration_url
        message = str(exc_info.value)
        assert "integration_url" in message
        assert "access-gated" in message
        assert "User" in message

    def test_the_user_docstring_documents_access_tiers(self):
        assert "access tier" in User.__doc__.lower()
        assert "AttributeError" in User.__doc__


class TestGrantTrust:
    def test_grant_posts_and_updates_live_object(self, bc, api):
        api.get(f"/users/{NOVA['uuid']}").respond(
            200, json={"user": directory_user_payload(user=NOVA)}
        )
        route = api.post(f"/users/{NOVA['uuid']}/trust").respond(
            201, json={"user": directory_user_payload(user=NOVA, you_trust=True)}
        )

        nova = bc.users.get(NOVA["uuid"])
        assert nova.trust.you_trust is False

        nova.grant_trust()

        assert route.called
        assert nova.trust.you_trust is True
        assert nova.trust.mutual is False  # they haven't trusted you back yet

    def test_grant_is_idempotent(self, bc, api):
        api.get("/users").respond(
            200, json={"users": [directory_user_payload(user=NOVA, you_trust=True)]}
        )
        api.post(f"/users/{NOVA['uuid']}/trust").respond(
            201, json={"user": directory_user_payload(user=NOVA, you_trust=True)}
        )

        (nova,) = bc.users
        nova.grant_trust()  # re-granting existing trust succeeds

        assert nova.trust.you_trust is True

    def test_grant_to_unknown_user_raises_not_found(self, bc, api):
        api.get("/users").respond(200, json={"users": [directory_user_payload(user=NOVA)]})
        api.post(f"/users/{NOVA['uuid']}/trust").respond(404, json=problem("not_found", 404))

        (nova,) = bc.users
        with pytest.raises(NotFoundError):
            nova.grant_trust()


class TestRevokeTrust:
    def test_revoke_deletes_and_updates_live_object(self, bc, api):
        api.get("/users").respond(
            200,
            json={"users": [directory_user_payload(user=NOVA, you_trust=True, trusts_you=True)]},
        )
        route = api.delete(f"/users/{NOVA['uuid']}/trust").respond(204)

        (nova,) = bc.users
        assert nova.trust.mutual is True

        nova.revoke_trust()

        assert route.called
        assert nova.trust.you_trust is False
        assert nova.trust.mutual is False
        # The reverse edge is theirs, not yours — untouched.
        assert nova.trust.trusts_you is True

    def test_revoke_is_idempotent(self, bc, api):
        api.get("/users").respond(200, json={"users": [directory_user_payload(user=NOVA)]})
        api.delete(f"/users/{NOVA['uuid']}/trust").respond(204)

        (nova,) = bc.users
        nova.revoke_trust()  # revoking trust you don't hold still returns 204

        assert nova.trust.you_trust is False


class TestOneUserClassEverywhere:
    """Acceptance criterion: THE User class — the same one, everywhere a user appears."""

    def test_directory_dashboard_get_and_timeline_owner_share_one_class(self, bc, api):
        api.get("/users").respond(200, json={"users": [directory_user_payload(user=NOVA)]})
        api.get(f"/users/{JOHN['uuid']}").respond(
            200, json={"user": trusted_peer_user_payload(user=JOHN)}
        )
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)
        api.get(f"/timelines/{TIMELINE_UUID}").respond(
            200, json={"timeline": timeline_payload(), "items": []}
        )

        (directory_row,) = bc.users
        fetched = bc.users.get(JOHN["uuid"])
        dashboard_you = bc.me.you
        timeline_owner = bc.timelines.get(TIMELINE_UUID).owner

        assert (
            type(directory_row)
            is type(fetched)
            is type(dashboard_you)
            is type(timeline_owner)
            is User
        )


class TestTheHandshake:
    """The full consent flow: discover → grant trust → (mutual) → share a timeline."""

    def test_directory_to_participation(self, bc, api):
        api.get("/users").respond(
            200, json={"users": [directory_user_payload(user=NOVA, trusts_you=True)]}
        )
        api.post(f"/users/{NOVA['uuid']}/trust").respond(
            201,
            json={"user": trusted_peer_user_payload(user=NOVA, you_trust=True, trusts_you=True)},
        )
        api.post("/timelines").respond(
            201, json={"timeline": timeline_payload(participants=[]), "items": []}
        )
        api.post(f"/timelines/{TIMELINE_UUID}/participations").respond(201, json=NOVA)

        # 1. Find Nova in the directory — she already trusts us, we don't trust her yet.
        (nova,) = bc.users
        assert nova.trust.trusts_you is True
        assert nova.trust.mutual is False

        # 2. Grant our half of the handshake — now it's mutual.
        nova.grant_trust()
        assert nova.trust.mutual is True

        # 3. Mutual trust opens the gate: share a timeline.
        timeline = bc.timelines.create(name="Incident response")
        added = timeline.add_participant(nova)
        assert added.handle == "nova"
        assert [p.handle for p in timeline.participants] == ["nova"]
