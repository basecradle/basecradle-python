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
from typing import Any

from basecradle._items import (
    AsyncItemsResource,
    ItemsResource,
    _idempotency_headers,
    _NestedCreatorCore,
)
from basecradle._models import ApiObject
from basecradle._users import User

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


class WebhookEventContent(ApiObject):
    """One inbound delivery: what was sent, and the two facts fixed when it arrived.

    ``ingest_token_at_receipt`` and ``verified_at_receipt`` are the event's only
    **historical** fields — the endpoint as it was at delivery time. Everything inside the
    embedded ``webhook_endpoint`` is **current**, so comparing the token here with the one
    at the end of ``event.webhook_endpoint.content.ingest_url`` tells you whether the
    endpoint has rotated since.

    ``headers`` is the delivery's request headers, one pair per header in wire spelling,
    ``Content-Type`` and ``Content-Length`` included — but **the sender's casing is not
    preserved**: names arrive canonicalized to Title-Case per segment, and the SDK hands
    them back exactly as the wire gave them. A vendor's own published spelling can
    therefore miss: ``headers["X-Github-Delivery"]`` reads GitHub's delivery id, while
    ``headers["X-GitHub-Delivery"]`` — the spelling GitHub itself publishes — raises
    ``KeyError``. Match case-insensitively instead. ``httpx`` is already this SDK's only
    runtime dependency, so its header mapping is the one-expression way to do it::

        httpx.Headers(event.content.headers)["x-github-delivery"]
    """

    uuid: str
    content_type: str
    headers: dict
    payload: str  # the raw request body, exactly as delivered
    ingest_token_at_receipt: str  # the (possibly now-retired) ingest token it arrived on
    verified_at_receipt: bool  # was the delivery's signature verified on arrival?


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
