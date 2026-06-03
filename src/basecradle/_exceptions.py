"""Typed exceptions for every error the SDK can raise.

Every API error is an RFC 9457 ``application/problem+json`` document with a stable,
machine-readable ``code``. Each code maps to its own exception class, organized under
broader categories — catch as narrowly or as broadly as you like. ``BaseCradleError``
is the root: catching it catches everything this SDK raises.
"""

from __future__ import annotations

from typing import Any

import httpx

__all__ = [
    "BaseCradleError",
    "MissingTokenError",
    "APIConnectionError",
    "AuthenticationError",
    "UnauthorizedError",
    "InvalidCredentialsError",
    "InvalidSignatureError",
    "AccountSuspendedError",
    "ForbiddenError",
    "NotAViewerError",
    "NotTimelineOwnerError",
    "TimelineLockedError",
    "NotFoundError",
    "ValidationError",
    "CurrentPasswordIncorrectError",
    "PasswordConfirmationMismatchError",
    "RateLimitedError",
    "InvalidRequestError",
    "InvalidCursorError",
    "InvalidFilterError",
    "EndpointDisabledError",
    "PayloadTooLargeError",
]


class BaseCradleError(Exception):
    """Root of every exception this SDK raises.

    For errors that came from an API response, the problem document is exposed:
    ``status``, ``code``, ``title``, ``detail``, ``instance``, and ``problem`` (the
    full parsed document). For errors that never reached the API (missing token,
    connection failure), those attributes are ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        instance: str | None = None,
        problem: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.instance = instance
        self.problem = problem


class MissingTokenError(BaseCradleError):
    """No token was provided and ``BASECRADLE_TOKEN`` is not set."""


class APIConnectionError(BaseCradleError):
    """The request never got an API response (DNS failure, refused connection, timeout).

    The underlying ``httpx`` exception is preserved as ``__cause__``.
    """


# --- 401: authentication ---------------------------------------------------------------


class AuthenticationError(BaseCradleError):
    """Authentication failed (HTTP 401)."""


class UnauthorizedError(AuthenticationError):
    """``unauthorized`` — the Bearer token is missing or invalid."""


class InvalidCredentialsError(AuthenticationError):
    """``invalid_credentials`` — sign-in failed: the email address or password is wrong."""


class InvalidSignatureError(AuthenticationError):
    """``invalid_signature`` — webhook ingest: the signature is missing or does not match."""


# --- 403: account / permissions --------------------------------------------------------


class AccountSuspendedError(BaseCradleError):
    """``account_suspended`` — credentials were valid but the account is suspended."""


class ForbiddenError(BaseCradleError):
    """Authenticated but not allowed (HTTP 403)."""


class NotAViewerError(ForbiddenError):
    """``not_a_viewer`` — you are not a viewer (owner or participant) of the timeline."""


class NotTimelineOwnerError(ForbiddenError):
    """``not_timeline_owner`` — the action requires being the timeline's owner."""


class TimelineLockedError(ForbiddenError):
    """``timeline_locked`` — the timeline is locked and not accepting new content."""


# --- 404 --------------------------------------------------------------------------------


class NotFoundError(BaseCradleError):
    """``not_found`` — no record exists for the given UUID (or it is hidden from you)."""


# --- 422: validation --------------------------------------------------------------------


class ValidationError(BaseCradleError):
    """A submitted record failed validation (HTTP 422).

    ``errors`` maps attribute name → list of messages (empty when the API sent none).
    """

    def __init__(self, message: str, *, errors: dict[str, list[str]] | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.errors = errors or {}


class CurrentPasswordIncorrectError(ValidationError):
    """``current_password_incorrect`` — password change: the current password is incorrect."""


class PasswordConfirmationMismatchError(ValidationError):
    """``password_confirmation_mismatch`` — the new password and its confirmation differ."""


# --- 429 --------------------------------------------------------------------------------


class RateLimitedError(BaseCradleError):
    """``rate_limited`` — too many requests in the window.

    ``retry_after`` is the number of seconds to wait (from the ``Retry-After`` header),
    or ``None`` if the header was absent.
    """

    def __init__(self, message: str, *, retry_after: int | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


# --- 400: malformed requests ------------------------------------------------------------


class InvalidRequestError(BaseCradleError):
    """The request was malformed (HTTP 400)."""


class InvalidCursorError(InvalidRequestError):
    """``invalid_cursor`` — the ``before`` pagination cursor is not a valid record UUID."""


class InvalidFilterError(InvalidRequestError):
    """``invalid_filter`` — a list filter value is malformed."""


# --- webhook ingest ----------------------------------------------------------------------


class EndpointDisabledError(BaseCradleError):
    """``endpoint_disabled`` — webhook ingest: the endpoint is not accepting deliveries."""


class PayloadTooLargeError(BaseCradleError):
    """``payload_too_large`` — webhook ingest: the request body exceeds the maximum size."""


# --- the code → class registry -----------------------------------------------------------

_CODE_TO_ERROR: dict[str, type[BaseCradleError]] = {
    "unauthorized": UnauthorizedError,
    "invalid_credentials": InvalidCredentialsError,
    "invalid_signature": InvalidSignatureError,
    "account_suspended": AccountSuspendedError,
    "not_a_viewer": NotAViewerError,
    "not_timeline_owner": NotTimelineOwnerError,
    "timeline_locked": TimelineLockedError,
    "not_found": NotFoundError,
    "validation_failed": ValidationError,
    "current_password_incorrect": CurrentPasswordIncorrectError,
    "password_confirmation_mismatch": PasswordConfirmationMismatchError,
    "rate_limited": RateLimitedError,
    "invalid_cursor": InvalidCursorError,
    "invalid_filter": InvalidFilterError,
    "endpoint_disabled": EndpointDisabledError,
    "payload_too_large": PayloadTooLargeError,
}


def exception_from_response(response: httpx.Response) -> BaseCradleError:
    """Build the right exception for a non-2xx API response.

    Pure function of the response — the async client reuses it unchanged.

    Unknown codes fall back to ``BaseCradleError`` (the API is additive-only; a new
    error code must never crash the SDK), and non-problem+json bodies (e.g. a proxy's
    HTML error page) produce a ``BaseCradleError`` carrying just the HTTP status.
    """
    try:
        problem = response.json()
    except ValueError:
        problem = None

    if not isinstance(problem, dict) or "code" not in problem:
        return BaseCradleError(
            f"API request failed with HTTP {response.status_code}",
            status=response.status_code,
            problem=problem if isinstance(problem, dict) else None,
        )

    code = problem.get("code")
    detail = problem.get("detail")
    title = problem.get("title")
    message = detail or title or f"API request failed with HTTP {response.status_code}"

    common: dict[str, Any] = {
        "status": problem.get("status", response.status_code),
        "code": code,
        "title": title,
        "detail": detail,
        "instance": problem.get("instance"),
        "problem": problem,
    }

    error_class = _CODE_TO_ERROR.get(code, BaseCradleError)

    if issubclass(error_class, ValidationError):
        return error_class(message, errors=problem.get("errors"), **common)
    if issubclass(error_class, RateLimitedError):
        retry_after_header = response.headers.get("Retry-After")
        retry_after = int(retry_after_header) if retry_after_header is not None else None
        return error_class(message, retry_after=retry_after, **common)
    return error_class(message, **common)
