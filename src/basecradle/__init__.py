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
from basecradle._items import (
    Asset,
    AssetContent,
    AssetFile,
    AssetsResource,
    Item,
    ItemsResource,
    Message,
    MessageContent,
    MessagesResource,
    Task,
    TaskContent,
    TasksResource,
)
from basecradle._models import ApiObject
from basecradle._timelines import Timeline, TimelineItem, TimelinesResource
from basecradle._users import Trust, User
from basecradle._version import __version__

__all__ = [
    "__version__",
    "BaseCradle",
    # Models
    "ApiObject",
    "Asset",
    "AssetContent",
    "AssetFile",
    "AssetsResource",
    "Dashboard",
    "DashboardAccount",
    "DashboardDocumentation",
    "DashboardEnvironment",
    "DashboardInteraction",
    "DashboardTimelines",
    "Item",
    "ItemsResource",
    "Message",
    "MessageContent",
    "MessagesResource",
    "Task",
    "TaskContent",
    "TasksResource",
    "Timeline",
    "TimelineItem",
    "TimelinesResource",
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
