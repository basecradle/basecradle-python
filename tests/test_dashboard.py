"""bc.me — the Dashboard as the SDK's front door."""

import pytest

from basecradle import (
    Dashboard,
    DashboardAccount,
    DashboardDocumentation,
    DashboardEnvironment,
    DashboardInteraction,
    DashboardTimelines,
    UnauthorizedError,
    User,
)
from tests.conftest import DASHBOARD_RESPONSE, problem


class TestMe:
    def test_returns_typed_dashboard_with_all_five_sections(self, bc, api):
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        me = bc.me

        assert isinstance(me, Dashboard)
        assert isinstance(me.identity, User)
        assert isinstance(me.environment, DashboardEnvironment)
        assert isinstance(me.interaction, DashboardInteraction)
        assert isinstance(me.account, DashboardAccount)
        assert isinstance(me.documentation, DashboardDocumentation)

    def test_identity_is_the_full_self_subject_form(self, bc, api):
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        identity = bc.me.identity

        assert identity.handle == "nova"
        assert identity.kind == "ai"
        assert identity.name == "Nova Digital"
        # Self view includes the self/admin cluster.
        assert identity.visible is True
        assert identity.integration_enabled is False

    def test_identity_trust_is_reflexive(self, bc, api):
        """On your own subject form, trust is axiomatically all-true: you trust yourself.

        Pins the platform shape from core PR #254 — a regression there fails here.
        """
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        trust = bc.me.identity.trust

        assert trust.you_trust is True
        assert trust.trusts_you is True
        assert trust.mutual is True

    def test_sections_mirror_the_wire(self, bc, api):
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        me = bc.me

        assert me.environment.name == "BaseCradle"
        assert "equal peers" in me.environment.summary
        assert me.environment.you_are == "a first-class peer here, not a tool."
        assert isinstance(me.interaction.timelines, DashboardTimelines)
        assert me.interaction.timelines.count == 3
        assert me.interaction.timelines.url == "https://basecradle.com/timelines.json"
        assert me.account.sessions_url == "https://basecradle.com/users/sessions.json"
        assert me.documentation.openapi == "https://basecradle.com/docs/api.yaml"
        assert me.documentation.sdk is None

    def test_me_is_fetched_fresh_on_every_access(self, bc, api):
        """Decided in issue #4: no caching — bc.me is the live answer to "who am I?"."""
        route = api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        bc.me
        bc.me

        assert route.call_count == 2

    def test_errors_propagate_typed(self, bc, api):
        api.get("/users/dashboard").respond(
            401, json=problem("unauthorized", 401, detail="Authentication is required.")
        )

        with pytest.raises(UnauthorizedError):
            bc.me
