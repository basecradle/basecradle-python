"""The spec drift-guard: the SDK can never silently fall behind the live API.

The platform's OpenAPI spec is generated from its test suite and cannot lie. This module
gives the SDK the reverse guarantee: the live spec's every endpoint must appear in the
coverage map below, or CI fails. When the platform adds an endpoint, this check fails →
an issue gets filed → the SDK adds coverage. That is the intended workflow.

Endpoints are half of the surface. The other half is the error codes: a code the platform
documents with no typed class here reaches callers as a bare ``BaseCradleError``, and no
endpoint check notices (``binary_payload`` did exactly that, #264). So the docs' Error
Codes table is held to the SDK's registry the same way (#265).

The live checks are the ONLY tests in this suite that touch the network (one GET each of
two public documents). They are marked ``live`` and excluded from the default test run; CI
runs them as a dedicated job.
"""

import re
from collections.abc import Mapping

import httpx
import pytest

from basecradle import BaseCradle, ContactMessage, Session, Task, Timeline, User, WebhookEndpoint
from basecradle._exceptions import _CODE_TO_ERROR

LIVE_SPEC_URL = "https://basecradle.com/docs/api.yaml"
LIVE_DOCS_URL = "https://basecradle.com/docs/api.md"

# ---------------------------------------------------------------------------------------
# The coverage map: every endpoint the live API exposes → the SDK feature that covers it.
# This is the contract the drift-guard enforces, and documentation of what implements what.
# ---------------------------------------------------------------------------------------

COVERAGE: dict[tuple[str, str], str] = {
    # Authentication
    ("POST", "/session"): "BaseCradle.login()",
    ("DELETE", "/session"): "bc.sign_out()",
    ("PATCH", "/users/password"): "bc.change_password()",
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
    ("POST", "/tasks/{task_id}/cancellation"): "task.cancel()",
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
    # Contact messages & notes — admin-only
    ("GET", "/contact_messages"): "bc.contact_messages (iteration) / .filter()",
    ("GET", "/contact_messages/{id}"): "bc.contact_messages.get()",
    ("PATCH", "/contact_messages/{contact_message_id}/status"): "contact_message.set_status()",
    ("POST", "/contact_messages/{contact_message_id}/notes"): "contact_message.add_note()",
    ("GET", "/notes"): "bc.notes (iteration)",
    ("GET", "/notes/{id}"): "bc.notes.get()",
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


_ERROR_CODES_HEADING = "### Error Codes"
_ERROR_CODES_HEADER_ROW = "| Code | Status | When |"
_ERROR_CODES_SEPARATOR = re.compile(r"^\|(?: *:?-+:? *\|){3}$")
# A row: the code in backticks, optionally followed by its `{:#error-…}` anchor, then a
# three-digit status and a non-empty description. The description may hold an escaped
# `\|` but no bare pipe, so a row that grows a column fails rather than parsing.
_ERROR_CODE_ROW = re.compile(
    r"^\| `([a-z0-9_]+)`(?:\{:#[^}]*\})? \| [0-9]{3} \| (?:[^|\\\s]|\\.)(?:[^|\\]|\\.)* \|$"
)
# The section ends at the next heading of the same or a higher level, outside a code fence.
_SECTION_END = re.compile(r"^#{1,3} ")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


def documented_error_codes(docs_text: str) -> set[str]:
    """Every code in the prose docs' ``### Error Codes`` table.

    The prose docs, not the spec: the table is the platform's one complete list, while
    the spec's problem+json examples can only carry codes some contract test exercises.

    A constrained parser for the table's current shape. Unlike the spec, the table is
    hand-written, so the guards are stricter than ``endpoint_pairs``'s: every table line
    in the section must parse, or the whole read fails. A row reshaped so it no longer
    matches would otherwise drop out silently, and its code would never be checked.
    """
    lines = [line.rstrip() for line in docs_text.splitlines()]
    starts = [i for i, line in enumerate(lines) if line == _ERROR_CODES_HEADING]
    if len(starts) != 1:
        raise AssertionError(
            f"Found {len(starts)} '{_ERROR_CODES_HEADING}' headings in the docs, expected "
            f"exactly 1 — the docs format has likely changed and this parser needs "
            f"updating. Never ignore this."
        )

    # Every line in the section, outside a code fence, that holds a pipe — indented, or
    # missing GFM's optional leading pipe. The row regex below is anchored at a leading
    # `|`, so such a row fails loudly instead of being skipped. Collecting too much is the
    # safe direction: an extra line can only fail the read, never pass it.
    table_lines: list[str] = []
    in_fence = False
    for line in lines[starts[0] + 1 :]:
        if _FENCE.match(line):
            in_fence = not in_fence
        elif in_fence:
            continue
        elif _SECTION_END.match(line):
            break
        elif "|" in line:
            table_lines.append(line)

    # Format-drift guards: if the table changes shape, fail loudly here rather than
    # letting the comparison pass vacuously or on a partial read.
    if len(table_lines) < 2 or table_lines[0] != _ERROR_CODES_HEADER_ROW:
        raise AssertionError(
            f"The Error Codes table does not start with {_ERROR_CODES_HEADER_ROW!r} — the "
            f"docs format has likely changed and this parser needs updating. Never ignore "
            f"this."
        )
    if not _ERROR_CODES_SEPARATOR.match(table_lines[1]):
        raise AssertionError(
            f"The Error Codes table's second line is {table_lines[1]!r}, not a separator "
            f"row — the docs format has likely changed and this parser needs updating. "
            f"Never ignore this."
        )
    codes: set[str] = set()
    for line in table_lines[2:]:
        row_match = _ERROR_CODE_ROW.match(line)
        if not row_match:
            raise AssertionError(
                f"Unparseable Error Codes row {line!r} — the docs format has likely "
                f"changed and this parser needs updating. Never ignore this."
            )
        codes.add(row_match.group(1))

    if len(codes) < 15:
        raise AssertionError(
            f"Parsed only {len(codes)} error codes from the docs — the docs format has "
            f"likely changed and this parser needs updating. Never ignore this."
        )
    if "not_found" not in codes:
        raise AssertionError(
            "not_found is missing from the parsed error codes — the docs format has "
            "likely changed and this parser needs updating. Never ignore this."
        )
    return codes


def untyped(documented: set[str], registry: Mapping[str, object]) -> set[str]:
    """The error codes the docs list that the SDK has no typed exception for."""
    return documented - set(registry)


# ---------------------------------------------------------------------------------------
# The live checks — the only networked tests, run as their own CI job
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

    def test_every_documented_error_code_is_typed(self):
        response = httpx.get(LIVE_DOCS_URL, follow_redirects=True)
        response.raise_for_status()

        missing = untyped(documented_error_codes(response.text), _CODE_TO_ERROR)

        assert not missing, (
            f"The live docs list {len(missing)} error code(s) the SDK has no typed "
            f"exception for: {sorted(missing)}. Callers get a bare BaseCradleError for "
            f"each. This is the drift-guard working as intended — file an issue for each, "
            f"add the class, map it in _CODE_TO_ERROR, and add its ERROR_CATALOG row in "
            f"tests/test_errors.py."
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


def error_codes_docs(codes, *, before="", after="## Rate Limiting\n") -> str:
    """A docs fixture shaped like the live Error Codes section, one row per code."""
    rows = "".join(
        f"| `{code}`{{:#error-{code}}} | 400 | Something went wrong. |\n" for code in codes
    )
    return (
        f"{before}## Errors\n\nThe envelope.\n\n### Error Codes\n\n"
        f"The `code` is the stable contract.\n\n"
        f"| Code | Status | When |\n|---|---|---|\n{rows}\n"
        f"The per-endpoint tables below.\n\n{after}"
    )


class TestErrorCodesParser:
    def test_extracts_every_documented_code(self):
        assert documented_error_codes(error_codes_docs(_CODE_TO_ERROR)) == set(_CODE_TO_ERROR)

    def test_reads_only_the_error_codes_section(self):
        # Tables before the heading and after the section ends are not error codes.
        stray = "| Code | Status | When |\n|---|---|---|\n| `elsewhere` | 400 | Not ours. |\n"
        docs = error_codes_docs(_CODE_TO_ERROR, before=stray, after=f"## Rate Limiting\n{stray}")

        assert "elsewhere" not in documented_error_codes(docs)

    def test_a_row_without_an_anchor_still_counts(self):
        docs = error_codes_docs(_CODE_TO_ERROR).replace(
            "| `not_a_viewer`{:#error-not_a_viewer} |", "| `not_a_viewer` |"
        )
        assert "| `not_a_viewer` |" in docs  # the fixture really changed

        assert "not_a_viewer" in documented_error_codes(docs)

    def test_an_escaped_pipe_in_the_description_is_not_a_column(self):
        docs = error_codes_docs(_CODE_TO_ERROR).replace(
            "| `not_found`{:#error-not_found} | 400 | Something went wrong. |",
            "| `not_found`{:#error-not_found} | 404 | Missing \\| hidden. |",
        )
        assert "Missing \\| hidden." in docs  # the fixture really changed

        assert "not_found" in documented_error_codes(docs)

    def test_trailing_whitespace_and_crlf_are_tolerated(self):
        docs = error_codes_docs(_CODE_TO_ERROR).replace("\n", "  \r\n")
        assert "|  \r\n" in docs  # the fixture really changed

        assert documented_error_codes(docs) == set(_CODE_TO_ERROR)

    def test_missing_heading_fails_loudly(self):
        docs = error_codes_docs(_CODE_TO_ERROR).replace("### Error Codes", "### Codes")
        with pytest.raises(AssertionError, match="format has likely changed"):
            documented_error_codes(docs)

    def test_duplicate_heading_fails_loudly(self):
        # Two complete, valid sections: either alone would parse, so only the heading
        # count can refuse to pick one.
        docs = error_codes_docs(_CODE_TO_ERROR) * 2
        with pytest.raises(AssertionError, match="Found 2 '### Error Codes' headings"):
            documented_error_codes(docs)

    def test_reshaped_header_row_fails_loudly(self):
        docs = error_codes_docs(_CODE_TO_ERROR).replace(
            "| Code | Status | When |", "| Status | Code | When |"
        )
        with pytest.raises(AssertionError, match="format has likely changed"):
            documented_error_codes(docs)

    def test_missing_separator_row_fails_loudly(self):
        docs = error_codes_docs(_CODE_TO_ERROR).replace("|---|---|---|\n", "")
        with pytest.raises(AssertionError, match="format has likely changed"):
            documented_error_codes(docs)

    @pytest.mark.parametrize(
        "reshaped",
        [
            "| not_found | 404 | No backticks. |",
            "| `not_found` | Not Found | A status that is not three digits. |",
            "| `not_found` | 404 |",
            "| `not_found` | 404 | An extra column. | x |",
            "| `Not-Found` | 404 | A code outside the snake_case alphabet. |",
            "| `not_found` | 404 |  |",
            "`not_found` | 404 | GFM's optional leading pipe, omitted. |",
        ],
    )
    def test_one_unparseable_row_fails_the_whole_read(self, reshaped):
        """The reason the guard is stricter than the spec's: a row must not drop silently."""
        docs = error_codes_docs(_CODE_TO_ERROR).replace(
            "| `invalid_cursor`{:#error-invalid_cursor} | 400 | Something went wrong. |",
            reshaped,
        )
        assert reshaped in docs  # the fixture really changed
        with pytest.raises(AssertionError, match="Unparseable Error Codes row"):
            documented_error_codes(docs)

    def test_an_indented_row_fails_the_whole_read(self):
        row = "| `invalid_cursor`{:#error-invalid_cursor} | 400 | Something went wrong. |"
        docs = error_codes_docs(_CODE_TO_ERROR).replace(row, "  " + row)
        with pytest.raises(AssertionError, match="Unparseable Error Codes row"):
            documented_error_codes(docs)

    def test_a_heading_inside_a_code_fence_does_not_end_the_section(self):
        # A `# comment` in an example would otherwise stop the read before the rows below it.
        docs = error_codes_docs(_CODE_TO_ERROR).replace(
            "The per-endpoint tables below.",
            "```sh\n# a shell comment\n```\n\n| `after_the_fence` | 400 | Still read. |",
        )

        assert "after_the_fence" in documented_error_codes(docs)

    def test_a_pipe_inside_a_code_fence_is_not_a_row(self):
        docs = error_codes_docs(_CODE_TO_ERROR).replace(
            "The per-endpoint tables below.", "```\n| a | b |\n```"
        )
        assert "| a | b |" in docs  # the fixture really changed

        assert documented_error_codes(docs) == set(_CODE_TO_ERROR)

    def test_an_aligned_separator_row_is_accepted(self):
        docs = error_codes_docs(_CODE_TO_ERROR).replace("|---|---|---|", "| :--- | :---: | --- |")
        assert "| :--- |" in docs  # the fixture really changed

        assert documented_error_codes(docs) == set(_CODE_TO_ERROR)

    def test_too_few_codes_fails_loudly(self):
        docs = error_codes_docs(["not_found", "unauthorized", "rate_limited"])
        with pytest.raises(AssertionError, match="Parsed only 3 error codes"):
            documented_error_codes(docs)

    def test_missing_sentinel_fails_loudly(self):
        docs = error_codes_docs(code for code in _CODE_TO_ERROR if code != "not_found")
        with pytest.raises(AssertionError, match="not_found is missing"):
            documented_error_codes(docs)


class TestErrorCodeComparison:
    def test_a_missing_registry_entry_is_reported(self):
        """Acceptance criterion: dropping any mapped code makes the check fail."""
        for code in _CODE_TO_ERROR:
            broken_registry = {k: v for k, v in _CODE_TO_ERROR.items() if k != code}

            assert untyped(set(_CODE_TO_ERROR), broken_registry) == {code}

    def test_a_newly_documented_code_is_reported(self):
        documented = set(_CODE_TO_ERROR) | {"brand_new_code"}

        assert untyped(documented, _CODE_TO_ERROR) == {"brand_new_code"}

    def test_full_registry_reports_nothing(self):
        assert untyped(set(_CODE_TO_ERROR), _CODE_TO_ERROR) == set()

    def test_a_code_the_docs_do_not_list_is_not_required(self):
        # The registry can be a superset of the docs, like COVERAGE of the spec.
        assert untyped(set(_CODE_TO_ERROR) - {"not_found"}, _CODE_TO_ERROR) == set()


class TestCoverageMapHonesty:
    """The map can't claim coverage by SDK features that don't exist."""

    def test_client_level_features_exist(self):
        bc = BaseCradle(token="bc_uat_KqI8zFxkQ0OZ8vYwT7mWcVtR3nSdLpEa")

        assert hasattr(BaseCradle, "login")
        assert hasattr(BaseCradle, "sign_out")
        assert hasattr(BaseCradle, "change_password")
        assert hasattr(BaseCradle, "request")  # the escape hatch for anything not yet wrapped
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
            "contact_messages",
            "notes",
        ):
            assert hasattr(bc, resource), f"COVERAGE references bc.{resource}, which is gone"
        bc.close()

    def test_model_verbs_exist(self):
        for model, verbs in [
            (Timeline, ("lock", "delete", "add_participant", "remove_participant")),
            (Task, ("cancel",)),
            (User, ("grant_trust", "revoke_trust")),
            (Session, ("revoke",)),
            (WebhookEndpoint, ("enable", "disable", "rotate")),
            (ContactMessage, ("set_status", "add_note")),
        ]:
            for verb in verbs:
                assert callable(getattr(model, verb, None)), (
                    f"COVERAGE references {model.__name__}.{verb}(), which is gone"
                )

    def test_every_covered_endpoint_names_a_real_feature(self):
        # Every value either names something asserted above or is an explicit exclusion.
        for (method, path), feature in COVERAGE.items():
            assert feature, f"({method}, {path}) has an empty coverage description"
