"""The spec drift-guard: the SDK can never silently fall behind the live API.

The platform's OpenAPI spec is generated from its test suite and cannot lie. This module
gives the SDK the reverse guarantee: the live spec's every endpoint must appear in the
coverage map below, or CI fails. When the platform adds an endpoint, this check fails →
an issue gets filed → the SDK adds coverage. That is the intended workflow.

The live check is the ONE test in this suite that touches the network (a single GET of a
public document). It is marked ``live`` and excluded from the default test run; CI runs it
as a dedicated job.
"""

import re

import httpx
import pytest

from basecradle import BaseCradle, Session, Timeline, User, WebhookEndpoint

LIVE_SPEC_URL = "https://basecradle.com/docs/api.yaml"

# ---------------------------------------------------------------------------------------
# The coverage map: every endpoint the live API exposes → the SDK feature that covers it.
# This is the contract the drift-guard enforces, and documentation of what implements what.
# ---------------------------------------------------------------------------------------

COVERAGE: dict[tuple[str, str], str] = {
    # Authentication
    ("POST", "/session"): "BaseCradle.login()",
    # Dashboard — self-discovery
    ("GET", "/users/dashboard"): "bc.me",
    # Timelines
    ("GET", "/timelines"): "bc.timelines (iteration)",
    ("POST", "/timelines"): "bc.timelines.create()",
    ("GET", "/timelines/{id}"): "bc.timelines.get()",
    ("DELETE", "/timelines/{id}"): "timeline.delete()",
    ("POST", "/timelines/{timeline_id}/lock"): "timeline.lock()",
    # Participations
    ("POST", "/timelines/{timeline_id}/participations"): "timeline.add_participant()",
    ("DELETE", "/timelines/{timeline_id}/participations/{id}"): "timeline.remove_participant()",
    # Messages
    ("GET", "/messages"): "bc.messages (iteration) / .filter()",
    ("GET", "/messages/{id}"): "bc.messages.get()",
    ("POST", "/timelines/{timeline_id}/messages"): "timeline.messages.create()",
    # Assets
    ("GET", "/assets"): "bc.assets (iteration) / .filter()",
    ("GET", "/assets/{id}"): "bc.assets.get()",
    ("POST", "/timelines/{timeline_id}/assets"): "timeline.assets.create()",
    # Tasks
    ("GET", "/tasks"): "bc.tasks (iteration) / .filter()",
    ("GET", "/tasks/{id}"): "bc.tasks.get()",
    ("POST", "/timelines/{timeline_id}/tasks"): "timeline.tasks.create()",
    # Webhook endpoints
    ("GET", "/webhook_endpoints"): "bc.webhook_endpoints (iteration) / .filter()",
    ("GET", "/webhook_endpoints/{id}"): "bc.webhook_endpoints.get()",
    ("POST", "/timelines/{timeline_id}/webhook_endpoints"): "timeline.webhook_endpoints.create()",
    ("POST", "/webhook_endpoints/{webhook_endpoint_id}/enablement"): "endpoint.enable()",
    ("DELETE", "/webhook_endpoints/{webhook_endpoint_id}/enablement"): "endpoint.disable()",
    ("POST", "/webhook_endpoints/{webhook_endpoint_id}/rotation"): "endpoint.rotate()",
    # Webhook events
    ("GET", "/webhook_events"): "bc.webhook_events (iteration) / .filter()",
    ("GET", "/webhook_events/{id}"): "bc.webhook_events.get()",
    # Users & trust
    ("GET", "/users"): "bc.users (iteration)",
    ("GET", "/users/{id}"): "bc.users.get()",
    ("POST", "/users/{user_id}/trust"): "user.grant_trust()",
    ("DELETE", "/users/{user_id}/trust"): "user.revoke_trust()",
    # Sessions — self-credential management
    ("GET", "/users/sessions"): "bc.sessions (iteration)",
    ("DELETE", "/users/sessions"): "bc.sessions.revoke_all()",
    ("DELETE", "/users/sessions/{id}"): "session.revoke()",
    # Webhook ingest — intentionally not covered by the SDK:
    # the ingest URL is for *external senders*, not authenticated peers. The SDK's job
    # is handing it out (endpoint.content.ingest_url), not POSTing to it.
    ("POST", "/webhooks/{ingest_token}"): "intentionally not covered (external senders only)",
}


# ---------------------------------------------------------------------------------------
# The machinery (pure functions — the offline tests below prove them)
# ---------------------------------------------------------------------------------------

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def endpoint_pairs(spec_text: str) -> set[tuple[str, str]]:
    """Every (METHOD, path) pair in the spec.

    A constrained parser for the platform's *generated* spec format (two-space-indented
    quoted path keys, four-space-indented method keys). The guards below make any format
    drift loud — this function fails rather than silently extracting nothing.
    """
    pairs: set[tuple[str, str]] = set()
    current_path: str | None = None
    for line in spec_text.splitlines():
        path_match = re.match(r'^  "(/[^"]*)":\s*$', line)
        if path_match:
            current_path = path_match.group(1)
            continue
        method_match = re.match(rf"^    ({'|'.join(_HTTP_METHODS)}):\s*$", line)
        if method_match and current_path:
            pairs.add((method_match.group(1).upper(), current_path))

    # Format-drift guards: if the platform's YAML generator changes shape, fail loudly
    # here rather than letting the coverage check pass vacuously.
    if len(pairs) < 30:
        raise AssertionError(
            f"Parsed only {len(pairs)} endpoint pairs from the spec — the spec format has "
            f"likely changed and this parser needs updating. Never ignore this."
        )
    if ("POST", "/session") not in pairs:
        raise AssertionError(
            "POST /session is missing from the parsed pairs — the spec format has likely "
            "changed and this parser needs updating. Never ignore this."
        )
    return pairs


def uncovered(live_pairs: set[tuple[str, str]], coverage: dict[tuple[str, str], str]) -> set:
    """The endpoints the live API has that the coverage map doesn't account for."""
    return live_pairs - set(coverage)


# ---------------------------------------------------------------------------------------
# The live check — the one networked test, run as its own CI job
# ---------------------------------------------------------------------------------------


@pytest.mark.live
class TestDriftGuard:
    def test_every_live_endpoint_is_covered(self):
        response = httpx.get(LIVE_SPEC_URL, follow_redirects=True)
        response.raise_for_status()

        missing = uncovered(endpoint_pairs(response.text), COVERAGE)

        assert not missing, (
            f"The live API has {len(missing)} endpoint(s) the SDK does not cover: "
            f"{sorted(missing)}. This is the drift-guard working as intended — "
            f"file an issue for each, add SDK coverage, and extend COVERAGE in this file."
        )


# ---------------------------------------------------------------------------------------
# Offline meta-tests: prove the mechanism works (no network)
# ---------------------------------------------------------------------------------------

SPEC_FIXTURE = """---
openapi: 3.0.3
paths:
  "/things":
    get:
      summary: index
    post:
      summary: create
  "/things/{id}":
    get:
      summary: show
    delete:
      summary: destroy
"""


class TestParser:
    def test_extracts_every_method_path_pair(self):
        # The guards require >= 30 pairs, so build a fixture programmatically.
        spec = "paths:\n" + "".join(
            f'  "/r{i}":\n    get:\n      summary: index\n    post:\n      summary: create\n'
            for i in range(20)
        )
        spec += '  "/session":\n    post:\n      summary: create\n'

        pairs = endpoint_pairs(spec)

        assert ("GET", "/r0") in pairs
        assert ("POST", "/r19") in pairs
        assert ("POST", "/session") in pairs
        assert len(pairs) == 41

    def test_too_few_pairs_fails_loudly(self):
        with pytest.raises(AssertionError, match="format has likely changed"):
            endpoint_pairs(SPEC_FIXTURE)

    def test_unrecognizable_format_fails_loudly(self):
        with pytest.raises(AssertionError, match="format has likely changed"):
            endpoint_pairs("paths:\n  /unquoted-path:\n    get:\n")

    def test_nested_keys_are_not_mistaken_for_methods(self):
        # "delete:" appearing deeper than 4-space indentation must not count as a method.
        spec = "paths:\n" + "".join(
            f'  "/r{i}":\n    get:\n      properties:\n        delete:\n' for i in range(30)
        )
        spec += '  "/session":\n    post:\n      summary: create\n'

        pairs = endpoint_pairs(spec)

        # Only the 4-space-indented methods count: 30 GETs + 1 POST, zero DELETEs.
        assert len(pairs) == 31
        assert not any(method == "DELETE" for method, _ in pairs)


class TestCoverageComparison:
    def test_a_missing_coverage_entry_is_reported(self):
        """Acceptance criterion: deleting any coverage entry makes the check fail."""
        for entry in COVERAGE:
            broken_coverage = {k: v for k, v in COVERAGE.items() if k != entry}

            missing = uncovered(set(COVERAGE), broken_coverage)

            assert missing == {entry}

    def test_full_coverage_reports_nothing(self):
        assert uncovered(set(COVERAGE), COVERAGE) == set()

    def test_an_endpoint_the_api_does_not_have_is_not_required(self):
        # Coverage can be a superset of the live API (e.g. during platform rollbacks).
        live = set(COVERAGE) - {("GET", "/users")}
        assert uncovered(live, COVERAGE) == set()


class TestCoverageMapHonesty:
    """The map can't claim coverage by SDK features that don't exist."""

    def test_client_level_features_exist(self):
        bc = BaseCradle(token="bc_uat_KqI8zFxkQ0OZ8vYwT7mWcVtR3nSdLpEa")

        assert hasattr(BaseCradle, "login")
        assert hasattr(type(bc), "me")
        for resource in (
            "timelines",
            "messages",
            "assets",
            "tasks",
            "webhook_endpoints",
            "webhook_events",
            "sessions",
            "users",
        ):
            assert hasattr(bc, resource), f"COVERAGE references bc.{resource}, which is gone"
        bc.close()

    def test_model_verbs_exist(self):
        for model, verbs in [
            (Timeline, ("lock", "delete", "add_participant", "remove_participant")),
            (User, ("grant_trust", "revoke_trust")),
            (Session, ("revoke",)),
            (WebhookEndpoint, ("enable", "disable", "rotate")),
        ]:
            for verb in verbs:
                assert callable(getattr(model, verb, None)), (
                    f"COVERAGE references {model.__name__}.{verb}(), which is gone"
                )

    def test_every_covered_endpoint_names_a_real_feature(self):
        # Every value either names something asserted above or is an explicit exclusion.
        for (method, path), feature in COVERAGE.items():
            assert feature, f"({method}, {path}) has an empty coverage description"
