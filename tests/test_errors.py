"""Every documented error code maps to its typed exception.

The catalog below mirrors the API docs (Errors → Error Codes) and is pinned to the SDK's
registry, so a mapping removed, or added without a test, fails here. A code the platform
*documents* after this release is not caught by anything: the drift-guard compares
endpoints, not codes (#264, #265). It reads as a bare ``BaseCradleError`` until it is mapped.
"""

import pytest

from basecradle import (
    AccountSuspendedError,
    AuthenticationError,
    BaseCradleError,
    BinaryPayloadError,
    ConflictError,
    CurrentPasswordIncorrectError,
    EndpointDisabledError,
    ForbiddenError,
    InvalidCredentialsError,
    InvalidCursorError,
    InvalidFilterError,
    InvalidRequestError,
    InvalidSignatureError,
    NotAnAdminError,
    NotAViewerError,
    NotFoundError,
    NotTaskAuthorError,
    NotTimelineOwnerError,
    PasswordConfirmationMismatchError,
    PayloadTooLargeError,
    RateLimitedError,
    TaskNotPendingError,
    TimelineLockedError,
    UnauthorizedError,
    ValidationError,
)
from basecradle._exceptions import _CODE_TO_ERROR
from tests.conftest import FAKE_INSTANCE, problem

# (code, http status, expected exception class, expected category parent)
ERROR_CATALOG = [
    ("validation_failed", 422, ValidationError, BaseCradleError),
    ("invalid_credentials", 401, InvalidCredentialsError, AuthenticationError),
    ("account_suspended", 403, AccountSuspendedError, BaseCradleError),
    ("rate_limited", 429, RateLimitedError, BaseCradleError),
    ("unauthorized", 401, UnauthorizedError, AuthenticationError),
    ("not_a_viewer", 403, NotAViewerError, ForbiddenError),
    ("not_timeline_owner", 403, NotTimelineOwnerError, ForbiddenError),
    ("not_task_author", 403, NotTaskAuthorError, ForbiddenError),
    ("not_an_admin", 403, NotAnAdminError, ForbiddenError),
    ("timeline_locked", 403, TimelineLockedError, ForbiddenError),
    ("not_found", 404, NotFoundError, BaseCradleError),
    ("task_not_pending", 409, TaskNotPendingError, ConflictError),
    ("invalid_cursor", 400, InvalidCursorError, InvalidRequestError),
    ("invalid_filter", 400, InvalidFilterError, InvalidRequestError),
    ("current_password_incorrect", 422, CurrentPasswordIncorrectError, ValidationError),
    ("password_confirmation_mismatch", 422, PasswordConfirmationMismatchError, ValidationError),
    ("invalid_signature", 401, InvalidSignatureError, AuthenticationError),
    ("endpoint_disabled", 410, EndpointDisabledError, BaseCradleError),
    ("payload_too_large", 413, PayloadTooLargeError, BaseCradleError),
    ("binary_payload", 415, BinaryPayloadError, BaseCradleError),
]


def test_the_catalog_is_exactly_the_registry():
    """Every mapped code has a row here, and every row is mapped — no count to keep in step."""
    assert {code for code, *_ in ERROR_CATALOG} == set(_CODE_TO_ERROR)
    assert len(ERROR_CATALOG) == len(_CODE_TO_ERROR)  # no duplicate rows


@pytest.mark.parametrize(("code", "status", "error_class", "category"), ERROR_CATALOG)
class TestErrorCatalog:
    def test_code_maps_to_typed_exception(self, bc, api, code, status, error_class, category):
        api.get("/users/dashboard").respond(status, json=problem(code, status))

        with pytest.raises(error_class) as exc_info:
            bc.request("GET", "/users/dashboard")

        error = exc_info.value
        assert isinstance(error, category)
        assert isinstance(error, BaseCradleError)
        assert type(error) is error_class

    def test_problem_document_is_exposed(self, bc, api, code, status, error_class, category):
        document = problem(code, status)
        api.get("/users/dashboard").respond(status, json=document)

        with pytest.raises(BaseCradleError) as exc_info:
            bc.request("GET", "/users/dashboard")

        error = exc_info.value
        assert error.status == status
        assert error.code == code
        assert error.title == document["title"]
        assert error.detail == document["detail"]
        assert error.instance == FAKE_INSTANCE
        assert error.problem == document
        # The exception message is the human-readable detail.
        assert str(error) == document["detail"]


class TestValidationErrors:
    def test_per_attribute_errors_exposed(self, bc, api):
        api.post("/timelines").respond(
            422,
            json=problem(
                "validation_failed",
                422,
                detail="Name can't be blank",
                errors={"name": ["can't be blank"]},
            ),
        )

        with pytest.raises(ValidationError) as exc_info:
            bc.request("POST", "/timelines", json={"name": ""})

        assert exc_info.value.errors == {"name": ["can't be blank"]}

    def test_errors_default_to_empty_dict(self, bc, api):
        # current_password_incorrect is a 422 without a per-attribute errors map.
        # PATCH, not POST: that is the only method the live spec gives /users/password.
        api.patch("/users/password").respond(422, json=problem("current_password_incorrect", 422))

        with pytest.raises(CurrentPasswordIncorrectError) as exc_info:
            bc.request("PATCH", "/users/password", json={})

        assert exc_info.value.errors == {}


class TestRateLimiting:
    def test_retry_after_from_header(self, bc, api):
        api.get("/users/dashboard").respond(
            429,
            json=problem("rate_limited", 429, detail="Rate limit exceeded. Retry after 42s."),
            headers={"Retry-After": "42"},
        )

        with pytest.raises(RateLimitedError) as exc_info:
            bc.request("GET", "/users/dashboard")

        assert exc_info.value.retry_after == 42

    def test_retry_after_none_when_header_absent(self, bc, api):
        api.get("/users/dashboard").respond(429, json=problem("rate_limited", 429))

        with pytest.raises(RateLimitedError) as exc_info:
            bc.request("GET", "/users/dashboard")

        assert exc_info.value.retry_after is None


class TestForwardCompatibility:
    """The API is additive-only: new error codes must never crash the SDK."""

    def test_unknown_code_raises_base_error(self, bc, api):
        api.get("/users/dashboard").respond(418, json=problem("brand_new_error_code", 418))

        with pytest.raises(BaseCradleError) as exc_info:
            bc.request("GET", "/users/dashboard")

        error = exc_info.value
        assert type(error) is BaseCradleError
        assert error.code == "brand_new_error_code"
        assert error.status == 418

    def test_non_problem_json_body(self, bc, api):
        # e.g. an intermediary proxy answering with an HTML error page.
        api.get("/users/dashboard").respond(502, text="<html>Bad Gateway</html>")

        with pytest.raises(BaseCradleError) as exc_info:
            bc.request("GET", "/users/dashboard")

        error = exc_info.value
        assert type(error) is BaseCradleError
        assert error.status == 502
        assert error.code is None

    def test_json_error_body_without_code(self, bc, api):
        api.get("/users/dashboard").respond(500, json={"message": "something broke"})

        with pytest.raises(BaseCradleError) as exc_info:
            bc.request("GET", "/users/dashboard")

        error = exc_info.value
        assert type(error) is BaseCradleError
        assert error.status == 500
        assert error.problem == {"message": "something broke"}

    @pytest.mark.parametrize(
        ("label", "code"),
        [
            ("a list", []),
            ("an object", {"nested": "code"}),
            ("a number", 42),
            ("a bool", True),
        ],
    )
    def test_a_code_that_is_not_a_string_does_not_crash(self, bc, api, label, code):
        """``code`` is whatever the wire sent, and the registry is keyed by ``str``.

        An unhashable value raised ``TypeError: unhashable type`` straight out of the
        registry lookup, so a malformed problem document crashed the SDK instead of
        producing the ``BaseCradleError`` this path promises -- and the caller got no
        status, no code and no problem document with which to work out why. Found by
        `mypy --strict`, which had been reporting the unnarrowed lookup all along (#229).
        """
        api.get("/users/dashboard").respond(500, json={"code": code, "detail": "broke"})

        with pytest.raises(BaseCradleError) as exc_info:
            bc.request("GET", "/users/dashboard")

        error = exc_info.value
        assert type(error) is BaseCradleError, label
        assert error.status == 500
        assert error.problem == {"code": code, "detail": "broke"}  # nothing is discarded
        assert str(error) == "broke"  # the rich path still applies

    def test_a_null_code_keeps_the_rich_problem_path(self, bc, api):
        """A regression guard on the fix above: ``None`` is hashable, so it never crashed.

        Narrowing to ``str`` must not quietly demote this case to the bare
        HTTP-status-only error -- ``detail`` is still the message, and the problem
        document is still attached.
        """
        api.get("/users/dashboard").respond(500, json={"code": None, "detail": "broke"})

        with pytest.raises(BaseCradleError) as exc_info:
            bc.request("GET", "/users/dashboard")

        error = exc_info.value
        assert error.code is None
        assert error.detail == "broke"
        assert str(error) == "broke"
        assert error.problem == {"code": None, "detail": "broke"}
