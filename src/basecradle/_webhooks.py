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

from basecradle._items import AsyncItemsResource, ItemsResource, _NestedCreatorCore
from basecradle._models import ApiObject

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

    Endpoints belong to the timeline, not a user, so there is no ``user`` block.
    Verbs update this object from the full endpoint the API returns (live objects).
    With ``AsyncBaseCradle``, await the verbs: ``await endpoint.rotate()``.
    """

    type: str  # "webhook_endpoint"
    created_at: str
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
    """One inbound delivery: what was sent, and on which (possibly retired) ingest URL."""

    uuid: str
    content_type: str
    headers: dict
    payload: str  # the raw request body, exactly as delivered
    ingest_token_at_receipt: str


class WebhookEvent(ApiObject):
    """One inbound delivery to a webhook endpoint. Read-only — produced by external senders."""

    type: str  # "webhook_event"
    created_at: str
    timeline: ApiObject  # reference form
    webhook_endpoint: ApiObject  # reference form — the event's direct container
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

    def create(self, *, description: str) -> WebhookEndpoint:
        """Create an inbound webhook endpoint on this timeline (viewer; unlocked)."""
        method, path, payload = _endpoint_request(self._timeline_uuid, description)
        return _endpoint_from(self._client.request(method, path, json=payload), self._client)

    def __iter__(self) -> Iterator[WebhookEndpoint]:
        return iter(WebhookEndpointsResource(self._client).filter(timeline=self._timeline_uuid))


class AsyncTimelineWebhookEndpoints(_NestedCreatorCore):
    """One timeline's webhook endpoints, async."""

    async def create(self, *, description: str) -> WebhookEndpoint:
        """Create an inbound webhook endpoint on this timeline (viewer; unlocked)."""
        method, path, payload = _endpoint_request(self._timeline_uuid, description)
        return _endpoint_from(await self._client.request(method, path, json=payload), self._client)

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
