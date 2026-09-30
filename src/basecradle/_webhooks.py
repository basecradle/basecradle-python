"""Webhook endpoints and events — how external services deliver into a timeline.

An **endpoint** is an inbound URL on a timeline: external services POST to its
``content.ingest_url`` and each delivery becomes an **event**. Endpoints are created and
managed through the API; events are read-only — they exist only because something was
delivered.

The API models enablement and rotation as singular state resources; the SDK expresses
them as verbs on the endpoint object — ``disable()``, ``enable()``, ``rotate()`` — never
as raw paths. With ``AsyncBaseCradle``, await the verbs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any, TypeVar, overload

from basecradle._items import (
    AsyncItemsResource,
    ItemsResource,
    _idempotency_headers,
    _NestedCreatorCore,
)
from basecradle._models import ApiObject
from basecradle._users import User

_Default = TypeVar("_Default")

__all__ = [
    "AsyncTimelineWebhookEndpoints",
    "AsyncTimelineWebhookEvents",
    "AsyncWebhookEndpointsResource",
    "AsyncWebhookEventsResource",
    "TimelineWebhookEndpoints",
    "TimelineWebhookEvents",
    "WebhookEndpoint",
    "WebhookEndpointContent",
    "WebhookEndpointsResource",
    "WebhookEvent",
    "WebhookEventContent",
    "WebhookEventHeaders",
    "WebhookEventsResource",
    "WebhookVerification",
]


# --- models -------------------------------------------------------------------------------


class WebhookVerification(ApiObject):
    """Whether inbound deliveries must be signed, and how."""

    enabled: bool
    signature_header: str
    verifier: str  # "hmac_sha256_hex"


class WebhookEndpointContent(ApiObject):
    """An endpoint's content: identity, state, and the rotatable ingest URL."""

    uuid: str  # the endpoint's stable identity — never changes
    description: str
    enabled: bool
    ingest_url: str  # the secret URL external senders POST to — rotatable
    verification: WebhookVerification


class WebhookEndpoint(ApiObject):
    """An inbound webhook URL on a timeline.

    An endpoint is **authored**: ``user`` is the peer who created it, in nested-actor form,
    and every event delivered here inherits that author. Verbs update this object from the
    full endpoint the API returns (live objects). With ``AsyncBaseCradle``, await the
    verbs: ``await endpoint.rotate()``.
    """

    type: str  # "webhook_endpoint"
    created_at: str
    updated_at: str  # moves when the description, enabled state or ingest URL changes
    user: User  # the endpoint's author, in nested-actor form
    timeline: ApiObject  # reference form — dereference via bc.timelines.get(...)
    content: WebhookEndpointContent

    def disable(self):
        """Soft-stop: refuse inbound deliveries (410 Gone) until re-enabled.

        The endpoint and its event history are kept; reversible via ``enable()``.
        With ``AsyncBaseCradle``, await this.
        """
        return self._verb("DELETE", self._enablement_path(), self._adopt)

    def enable(self):
        """Re-enable a disabled endpoint — inbound deliveries are accepted again.

        With ``AsyncBaseCradle``, await this.
        """
        return self._verb("POST", self._enablement_path(), self._adopt)

    def rotate(self):
        """Regenerate the ingest URL. The old URL dies immediately; the uuid is unchanged.

        Use this when an ingest URL leaks. Recorded events are preserved.
        With ``AsyncBaseCradle``, await this.
        """
        return self._verb("POST", f"/webhook_endpoints/{self.content.uuid}/rotation", self._adopt)

    def _enablement_path(self) -> str:
        return f"/webhook_endpoints/{self.content.uuid}/enablement"

    def _adopt(self, response: dict[str, Any]) -> None:
        """Live-object update: the API returned the complete endpoint; adopt it."""
        self._data.clear()
        self._data.update(response["webhook_endpoint"])


class WebhookEventHeaders(dict[str, str]):
    """One delivery's request headers: the wire's own spelling, looked up case-insensitively.

    A plain ``dict`` of exactly what the wire carried — one pair per header,
    ``Content-Type`` and ``Content-Length`` included — so iterating, ``keys()`` and ``==``
    all read the platform's own spelling and nothing is renamed. Only **lookup** folds
    case, because header names are case-insensitive by RFC and the platform does not
    preserve the sender's casing: it stores names canonicalized to Title-Case per segment
    (``X-Github-Delivery``, not ``X-GitHub-Delivery``). So a vendor's published spelling
    finds the header it names, and so does any other casing of it::

        event.content.headers["X-GitHub-Delivery"]  # GitHub's own published spelling
        event.content.headers["x-github-delivery"]  # the lowercase form
        event.content.headers["X-Github-Delivery"]  # what the wire actually carried

    ``in`` and ``get()`` fold case the same way, and ``copy()`` gives another of these. A
    header that was genuinely not delivered is **absent**, never ``None``: subscripting
    raises ``KeyError`` naming the headers that did arrive, and ``get()`` returns its
    default — plain ``dict`` behavior. Converting away from this type gives up the case
    folding (``dict(headers)``, ``{**headers}``, ``headers | other``), and so does writing
    to it: this is a read of one delivery that already happened, so the ``dict`` mutators
    are left exactly as ``dict`` defines them, case-sensitive.
    """

    def __getitem__(self, name: str) -> str:
        wire_name = self._wire_name(name)
        if wire_name is None:
            raise KeyError(
                f"No {name!r} header on this delivery. Header names are matched "
                f"case-insensitively, so no casing of it was delivered either. "
                f"Headers present: {sorted(self)}"
            )
        return super().__getitem__(wire_name)

    @overload
    def get(self, name: str) -> str | None: ...

    @overload
    def get(self, name: str, default: _Default) -> str | _Default: ...

    def get(self, name: str, default: Any = None) -> Any:
        """The header's value, matched case-insensitively, or ``default`` if not delivered.

        Overloaded rather than left at ``Any``: ``dict.get`` declares ``str | None`` with no
        default and ``str | _Default`` with one, and a ``py.typed`` SDK must not widen that
        — ``int(headers.get("Content-Length"))`` has to stay the type error it is.
        """
        wire_name = self._wire_name(name)
        return default if wire_name is None else super().__getitem__(wire_name)

    def __contains__(self, name: object) -> bool:
        return self._wire_name(name) is not None

    def copy(self) -> WebhookEventHeaders:
        """Another headers object — ``dict.copy()`` would silently downgrade to a plain dict."""
        return WebhookEventHeaders(self)

    def _wire_name(self, name: object) -> str | None:
        """The wire's own spelling of ``name``, or ``None`` if no casing of it was delivered.

        Anything but a string is simply not a header name, so it reads as absent rather than
        blowing up in ``str.lower`` — ``headers[object()]`` owes a ``KeyError``.
        """
        if not isinstance(name, str):
            return None
        if super().__contains__(name):
            return name
        folded = name.lower()
        return next((wire_name for wire_name in self if wire_name.lower() == folded), None)


class WebhookEventContent(ApiObject):
    """One inbound delivery: what was sent, and the two facts fixed when it arrived.

    ``ingest_token_at_receipt`` and ``verified_at_receipt`` are the event's only
    **historical** fields — the endpoint as it was at delivery time. Everything inside the
    embedded ``webhook_endpoint`` is **current**, so comparing the token here with the one
    at the end of ``event.webhook_endpoint.content.ingest_url`` tells you whether the
    endpoint has rotated since.

    ``headers`` is the delivery's request headers. The platform rewrites header names to
    canonical Title-Case per segment and its docs tell you to look them up
    case-insensitively, so this SDK hands back the wire's own pairs in an object whose
    lookup folds case: ``headers["X-GitHub-Delivery"]`` — GitHub's own published spelling —
    and ``headers["x-github-delivery"]`` both read the header the wire spells
    ``X-Github-Delivery``. See ``WebhookEventHeaders``.

    Reached instead through a ``webhook_event`` row of ``timeline.items``, ``content`` is
    the generic wire-exact object — a timeline item's content is a union of four record
    types, so it is not a ``WebhookEventContent`` — and ``item.content.headers`` is the
    plain dict the API returned. ``bc.webhook_events`` and ``timeline.webhook_events`` give
    the case-folding one.
    """

    uuid: str
    content_type: str
    headers: WebhookEventHeaders  # wire-exact pairs; lookup folds case
    payload: str  # the raw request body, exactly as delivered
    ingest_token_at_receipt: str  # the (possibly now-retired) ingest token it arrived on
    verified_at_receipt: bool  # was the delivery's signature verified on arrival?

    def _wrap(self, name: str, value: Any) -> Any:
        """As ``ApiObject``, plus the one field presented richer than its wire type."""
        if name == "headers" and isinstance(value, dict):
            return WebhookEventHeaders(value)
        return super()._wrap(name, value)


class WebhookEvent(ApiObject):
    """One inbound delivery to a webhook endpoint. Read-only — produced by external senders.

    An event has no author, so there is no ``user`` block — but its **endpoint** has one:
    read it at ``event.webhook_endpoint.user``. ``webhook_endpoint`` is the endpoint the
    delivery arrived on, embedded as a full ``WebhookEndpoint``: read its identity at
    ``event.webhook_endpoint.content.uuid``, reach its verbs directly
    (``event.webhook_endpoint.rotate()``), and pass it straight to
    ``bc.webhook_events.filter(endpoint=...)``.
    """

    type: str  # "webhook_event"
    created_at: str
    updated_at: str
    timeline: ApiObject  # reference form
    webhook_endpoint: WebhookEndpoint  # the event's direct container, embedded in full
    content: WebhookEventContent


# --- resources ----------------------------------------------------------------------------


class _WebhookEndpointsBinding:
    """Webhook endpoints from every timeline you can view, newest first."""

    _path = "/webhook_endpoints"
    _plural = "webhook_endpoints"
    _singular = "webhook_endpoint"
    _model = WebhookEndpoint


class _WebhookEventsBinding:
    """Webhook events from every timeline you can view, newest first."""

    _path = "/webhook_events"
    _plural = "webhook_events"
    _singular = "webhook_event"
    _model = WebhookEvent

    def filter(self, *, timeline: Any | None = None, endpoint: Any | None = None):
        """A new lazy resource narrowed by timeline and/or endpoint (objects or uuids)."""
        filters = self._merge_filters(timeline=timeline, endpoint=endpoint)  # type: ignore[attr-defined]
        return type(self)(self._client, filters=filters)  # type: ignore[attr-defined]


class WebhookEndpointsResource(_WebhookEndpointsBinding, ItemsResource): ...


class AsyncWebhookEndpointsResource(_WebhookEndpointsBinding, AsyncItemsResource): ...


class WebhookEventsResource(_WebhookEventsBinding, ItemsResource): ...


class AsyncWebhookEventsResource(_WebhookEventsBinding, AsyncItemsResource): ...


# --- nested resources on Timeline ---------------------------------------------------------


def _endpoint_request(timeline_uuid: str, description: str) -> tuple[str, str, dict[str, Any]]:
    return (
        "POST",
        f"/timelines/{timeline_uuid}/webhook_endpoints",
        {"webhook_endpoint": {"description": description}},
    )


def _endpoint_from(response: dict[str, Any], client: Any) -> WebhookEndpoint:
    return WebhookEndpoint(response["webhook_endpoint"], client=client)


class TimelineWebhookEndpoints(_NestedCreatorCore):
    """One timeline's webhook endpoints: create here, or iterate (newest first)."""

    def create(self, *, description: str, idempotency_key: str | None = None) -> WebhookEndpoint:
        """Create an inbound webhook endpoint on this timeline (viewer; unlocked).

        Pass ``idempotency_key`` (a UUID is ideal) to make the create safe to retry: a replay
        of the same key returns the original endpoint, never a duplicate. Endpoint keys are
        scoped per timeline **and author**, like every other keyed create. See the client's
        ``max_retries`` for opt-in automatic retry of keyed creates.
        """
        method, path, payload = _endpoint_request(self._timeline_uuid, description)
        response = self._client.request(
            method, path, json=payload, headers=_idempotency_headers(idempotency_key)
        )
        return _endpoint_from(response, self._client)

    def __iter__(self) -> Iterator[WebhookEndpoint]:
        return iter(WebhookEndpointsResource(self._client).filter(timeline=self._timeline_uuid))


class AsyncTimelineWebhookEndpoints(_NestedCreatorCore):
    """One timeline's webhook endpoints, async."""

    async def create(
        self, *, description: str, idempotency_key: str | None = None
    ) -> WebhookEndpoint:
        """Create an inbound webhook endpoint on this timeline. See ``TimelineWebhookEndpoints``."""
        method, path, payload = _endpoint_request(self._timeline_uuid, description)
        response = await self._client.request(
            method, path, json=payload, headers=_idempotency_headers(idempotency_key)
        )
        return _endpoint_from(response, self._client)

    def __aiter__(self) -> AsyncIterator[WebhookEndpoint]:
        return (
            AsyncWebhookEndpointsResource(self._client)
            .filter(timeline=self._timeline_uuid)
            .__aiter__()
        )


class TimelineWebhookEvents(_NestedCreatorCore):
    """One timeline's webhook events — read-only, so iterate is all there is."""

    def __iter__(self) -> Iterator[WebhookEvent]:
        return iter(WebhookEventsResource(self._client).filter(timeline=self._timeline_uuid))


class AsyncTimelineWebhookEvents(_NestedCreatorCore):
    """One timeline's webhook events, async — read-only."""

    def __aiter__(self) -> AsyncIterator[WebhookEvent]:
        return (
            AsyncWebhookEventsResource(self._client)
            .filter(timeline=self._timeline_uuid)
            .__aiter__()
        )
