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
        assert isinstance(me.you, User)
        assert isinstance(me.environment, DashboardEnvironment)
        assert isinstance(me.interaction, DashboardInteraction)
        assert isinstance(me.account, DashboardAccount)
        assert isinstance(me.documentation, DashboardDocumentation)

    def test_you_is_the_full_self_subject_form(self, bc, api):
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        you = bc.me.you

        assert you.handle == "nova"
        assert you.kind == "ai"
        assert you.name == "Nova Digital"
        # Self view includes the self/admin cluster.
        assert you.visible is True
        assert you.integration_enabled is False

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
