"""Every documented error code maps to its typed exception.

The catalog below mirrors the API docs (Errors → Error Codes) exactly — all 18 codes.
If the API adds a code, the drift-guard (issue #10) catches it; if someone removes a
mapping, these tests do.
"""

import pytest

from basecradle import (
    AccountSuspendedError,
    AuthenticationError,
    BaseCradleError,
    ConflictError,
    CurrentPasswordIncorrectError,
    EndpointDisabledError,
    ForbiddenError,
    InvalidCredentialsError,
    InvalidCursorError,
    InvalidFilterError,
    InvalidRequestError,
    InvalidSignatureError,
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
]


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
        api.post("/users/password").respond(422, json=problem("current_password_incorrect", 422))

        with pytest.raises(CurrentPasswordIncorrectError) as exc_info:
            bc.request("POST", "/users/password", json={})

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
