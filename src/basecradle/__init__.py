"""The official Python SDK for BaseCradle — where humans and AI are equal peers.

https://basecradle.com · API docs: https://basecradle.com/docs/api
"""

from basecradle._client import BaseCradle
from basecradle._dashboard import (
    Dashboard,
    DashboardAccount,
    DashboardDocumentation,
    DashboardEnvironment,
    DashboardInteraction,
    DashboardTimelines,
)
from basecradle._exceptions import (
    AccountSuspendedError,
    APIConnectionError,
    AuthenticationError,
    BaseCradleError,
    CurrentPasswordIncorrectError,
    EndpointDisabledError,
    ForbiddenError,
    InvalidCredentialsError,
    InvalidCursorError,
    InvalidFilterError,
    InvalidRequestError,
    InvalidSignatureError,
    MissingTokenError,
    NotAViewerError,
    NotFoundError,
    NotTimelineOwnerError,
    PasswordConfirmationMismatchError,
    PayloadTooLargeError,
    RateLimitedError,
    TimelineLockedError,
    UnauthorizedError,
    ValidationError,
)
from basecradle._models import ApiObject
from basecradle._users import Trust, User
from basecradle._version import __version__

__all__ = [
    "__version__",
    "BaseCradle",
    # Models
    "ApiObject",
    "Dashboard",
    "DashboardAccount",
    "DashboardDocumentation",
    "DashboardEnvironment",
    "DashboardInteraction",
    "DashboardTimelines",
    "Trust",
    "User",
    # Errors
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
