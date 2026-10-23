"""bc.me — the Dashboard as the SDK's front door."""

import httpx
import pytest

from basecradle import (
    APIConnectionError,
    BaseCradle,
    BaseCradleError,
    Dashboard,
    DashboardAccount,
    DashboardAdmin,
    DashboardDocumentation,
    DashboardEnvironment,
    DashboardInteraction,
    DashboardPagination,
    DashboardSdk,
    DashboardSdks,
    DashboardTimelines,
    DashboardTools,
    UnauthorizedError,
    User,
)
from tests.conftest import (
    ADMIN_DASHBOARD_RESPONSE,
    DASHBOARD_MD,
    DASHBOARD_RESPONSE,
    FAKE_TOKEN,
    problem,
)


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

    def test_an_admin_sees_a_typed_sixth_section(self, bc, api):
        api.get("/users/dashboard").respond(200, json=ADMIN_DASHBOARD_RESPONSE)

        admin = bc.me.admin

        assert isinstance(admin, DashboardAdmin)
        assert admin.contact_messages_url == "https://basecradle.com/contact_messages.json"
        assert admin.notes_url == "https://basecradle.com/notes.json"
        assert admin.guide_url == "https://basecradle.com/docs/api.md#contact-messages"

    def test_anyone_else_has_no_admin_section(self, bc, api):
        """Absent, not ``None``: the Dashboard's shape is how a reader learns its role."""
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        with pytest.raises(AttributeError, match="did not return 'admin'"):
            bc.me.admin

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
        assert me.environment.concepts_url == "https://basecradle.com/docs/api.md#concepts"
        assert isinstance(me.interaction.timelines, DashboardTimelines)
        assert me.interaction.timelines.count == 3
        assert me.interaction.timelines.url == "https://basecradle.com/timelines.json"
        assert me.account.sessions_url == "https://basecradle.com/users/sessions.json"
        assert me.documentation.openapi == "https://basecradle.com/docs/api.yaml"
        assert me.documentation.changelog == "https://basecradle.com/docs/changelog.md"

    def test_interaction_orientation_blocks_are_typed(self, bc, api):
        """``pagination`` and ``tools`` are objects, so they read like every other block.

        Both carry a ``summary`` the peer can act on without leaving the Dashboard, plus
        the link onward. Untyped they would come back as bare dicts — subscripting in a
        model layer whose whole promise is attribute access.
        """
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        interaction = bc.me.interaction

        assert isinstance(interaction.pagination, DashboardPagination)
        assert "newest-first, 50 per page" in interaction.pagination.summary
        assert interaction.pagination.guide_url == "https://basecradle.com/docs/api.md#pagination"

        assert isinstance(interaction.tools, DashboardTools)
        assert "thin wrappers over this same HTTP API" in interaction.tools.summary
        assert interaction.tools.mapping_url == (
            "https://basecradle.com/docs/api.md#tools-and-the-http-api"
        )

    def test_documentation_sdks_are_typed_by_language(self, bc, api):
        """The official SDKs, keyed by language, each entry an object of per-SDK pointers.

        Pins the platform shape from core PR #256 — a regression there fails here.
        """
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        sdks = bc.me.documentation.sdks

        assert isinstance(sdks, DashboardSdks)
        assert isinstance(sdks.python, DashboardSdk)
        assert sdks.python.repository == "https://github.com/basecradle/basecradle-python"
        assert sdks.python.package == "https://pypi.org/project/basecradle/"
        # A language is typed here once its SDK ships; Ruby's did.
        assert isinstance(sdks.ruby, DashboardSdk)
        assert sdks.ruby.package == "https://rubygems.org/gems/basecradle"

    def test_the_removed_sdk_slot_raises_the_standard_attribute_error(self, bc, api):
        """Core PR #256 removed `sdk` (the never-populated placeholder) from the wire.

        Reading it gets the standard "API did not return" AttributeError listing the
        real fields — never a silent None.
        """
        api.get("/users/dashboard").respond(200, json=DASHBOARD_RESPONSE)

        with pytest.raises(AttributeError, match="did not return 'sdk'"):
            bc.me.documentation.sdk

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


class TestDashboardMd:
    """bc.dashboard_md() — the Dashboard as Markdown, the page ``start_here`` names."""

    def test_returns_the_body_verbatim(self, bc, api):
        api.get("/users/dashboard.md").respond(
            200, text=DASHBOARD_MD, headers={"Content-Type": "text/markdown; charset=utf-8"}
        )

        primer = bc.dashboard_md()

        assert primer == DASHBOARD_MD  # leading/trailing whitespace and the em dash survive

    def test_a_body_that_looks_like_json_is_still_text(self, bc, api):
        """The accessor reads text; it never tries a body as JSON, whatever it looks like."""
        api.get("/users/dashboard.md").respond(200, text='{"identity": {"handle": "nova"}}')

        assert bc.dashboard_md() == '{"identity": {"handle": "nova"}}'

    def test_an_empty_body_is_an_empty_string_not_none(self, bc, api):
        """``request()`` maps an empty body to ``None``; a text read keeps the ``str`` type."""
        api.get("/users/dashboard.md").respond(200, text="")

        assert bc.dashboard_md() == ""

    def test_sends_the_clients_credentials_and_accepts_any_representation(self, bc, api):
        """The ``.md`` path picks the format, so the JSON-only default ``Accept`` is widened."""
        route = api.get("/users/dashboard.md").respond(200, text=DASHBOARD_MD)

        bc.dashboard_md()

        request = route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert request.headers["Accept"] == "*/*"
        assert request.headers["User-Agent"].startswith("basecradle-python/")

    def test_is_fetched_fresh_on_every_call(self, bc, api):
        route = api.get("/users/dashboard.md").respond(200, text=DASHBOARD_MD)

        bc.dashboard_md()
        bc.dashboard_md()

        assert route.call_count == 2

    def test_a_problem_document_raises_the_typed_error(self, bc, api):
        api.get("/users/dashboard.md").respond(
            401, json=problem("unauthorized", 401, detail="Authentication is required.")
        )

        with pytest.raises(UnauthorizedError) as exc_info:
            bc.dashboard_md()

        assert exc_info.value.code == "unauthorized"

    def test_a_bodyless_error_raises_with_its_status(self, bc, api):
        """A non-2xx with no problem document still raises; it is never returned as text.

        This is also the live shape today for a dead token on this path: the platform
        answers an unauthenticated ``.md`` request with a bodyless 406 (basecradle#714).
        """
        api.get("/users/dashboard.md").respond(406, text="")

        with pytest.raises(BaseCradleError) as exc_info:
            bc.dashboard_md()

        assert exc_info.value.status == 406

    def test_is_retried_like_any_get(self, api, monkeypatch):
        monkeypatch.setattr(BaseCradle, "_retry_backoff", staticmethod(lambda attempt: 0.0))
        route = api.get("/users/dashboard.md").mock(
            side_effect=[httpx.ReadTimeout("timed out"), httpx.Response(200, text=DASHBOARD_MD)]
        )

        with BaseCradle(token=FAKE_TOKEN, max_retries=1) as bc:
            assert bc.dashboard_md() == DASHBOARD_MD

        assert route.call_count == 2

    def test_a_transport_failure_raises_api_connection_error(self, bc, api):
        api.get("/users/dashboard.md").mock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(APIConnectionError):
            bc.dashboard_md()
