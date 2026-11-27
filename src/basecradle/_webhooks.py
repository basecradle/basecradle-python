"""Webhook endpoints and events — how external services deliver into a timeline.

An **endpoint** is an inbound URL on a timeline: external services POST to its
``content.ingest_url`` and each delivery becomes an **event**. Endpoints are created and
managed through the API; events are read-only — they exist only because something was
delivered.

The API models enablement and rotation as singular state resources, and signature
verification as a ``PATCH`` of the endpoint itself; the SDK expresses all of them as
verbs on the endpoint object — ``disable()``, ``enable()``, ``rotate()``,
``configure_verification()``, ``clear_verification()`` — never as raw paths. With
``AsyncBaseCradle``, await the verbs.
"""

from __future__ import annotations

from typing import Any, Self

from basecradle._headers import RequestHeaders
from basecradle._items import (
    AsyncItemsResource,
    ItemsResource,
    _idempotency_headers,
    _ItemsResourceCore,
    _NestedCreatorCore,
)
from basecradle._models import ApiObject
from basecradle._pagination import AsyncListIterator, ListIterator
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
    "WebhookEventHeaders",
    "WebhookEventsResource",
    "WebhookVerification",
]


# --- models -------------------------------------------------------------------------------


class WebhookVerification(ApiObject):
    """Whether inbound deliveries must prove they hold the endpoint's secret, and how.

    ``enabled`` is ``True`` once a secret is set. The secret itself is write-only:
    the API never returns it, so it is never here. Set it, the scheme and the header with
    ``endpoint.configure_verification(...)``; turn verification off with
    ``endpoint.clear_verification()``.
    """

    enabled: bool
    # The header that is read under "hmac_sha256_hex" (the signature's) and
    # "shared_secret_header" (the secret's). A "github", "slack", "stripe" or "twilio"
    # endpoint's header is fixed, and reported here: "X-Hub-Signature-256",
    # "X-Slack-Signature", "Stripe-Signature" or "X-Twilio-Signature". An "sns" endpoint reads
    # no header (SNS signs inside the message body), so it is an empty string there.
    signature_header: str
    # Today "hmac_sha256_hex", "github", "shared_secret_header", "slack", "stripe", "twilio" or
    # "sns" — an open set: read it as a string.
    verifier: str


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
    updated_at: str  # moves when the endpoint changes
    user: User  # the endpoint's author, in nested-actor form
    timeline: ApiObject  # reference form — dereference via bc.timelines.get(...)
    content: WebhookEndpointContent

    def disable(self) -> Any:
        """Soft-stop: refuse inbound deliveries (410 Gone) until re-enabled.

        The endpoint and its event history are kept; reversible via ``enable()``.
        With ``AsyncBaseCradle``, await this.
        """
        return self._verb("DELETE", self._enablement_path(), self._adopt)

    def enable(self) -> Any:
        """Re-enable a disabled endpoint — inbound deliveries are accepted again.

        With ``AsyncBaseCradle``, await this.
        """
        return self._verb("POST", self._enablement_path(), self._adopt)

    def rotate(self) -> Any:
        """Regenerate the ingest URL. The old URL dies immediately; the uuid is unchanged.

        Use this when an ingest URL leaks. Recorded events are preserved.
        With ``AsyncBaseCradle``, await this.
        """
        return self._verb("POST", f"{self._endpoint_path()}/rotation", self._adopt)

    def configure_verification(
        self,
        *,
        verifier: str | None = None,
        credential: str | None = None,
        signature_header: str | None = None,
    ) -> Any:
        """Set how inbound deliveries must prove the secret: the scheme, the secret, the header.

        Every argument is optional and only the ones you pass are sent, so anything left out
        stays as it is; pass at least one, or this raises ``TypeError`` before any request
        is made. ``verifier`` is the scheme — today ``"hmac_sha256_hex"`` (the default; any
        sender you control), ``"github"`` (a GitHub webhook), ``"shared_secret_header"`` (a
        sender that cannot sign: the named header carries the secret itself, which proves
        the sender holds it but does not sign the body), ``"slack"`` (a Slack app's Events
        API; the endpoint answers Slack's ``url_verification`` handshake itself, and that
        creates no event), ``"stripe"`` (a Stripe webhook destination. Stripe signs a
        timestamp with the body, and a delivery whose timestamp is more than five minutes
        from now is refused. A repeat of one Stripe event, the same event ``id``, is stored
        once), ``"twilio"`` (the webhooks Twilio sends: an incoming text message, a
        recording or transcription callback, a status callback. Twilio signs the URL as well
        as the body, so give it the ``ingest_url`` exactly as the endpoint reports it.
        Twilio reads the reply, so the endpoint answers each delivery it stores with
        ``200 OK`` and an empty TwiML document in place of the empty ``201``: that is its
        answer to Twilio, and nothing this SDK returns changes) or ``"sns"`` (an Amazon SNS
        topic. SNS signs inside the message body, with a certificate Amazon publishes, so
        there is no header. The endpoint confirms the SNS subscription itself, and that
        confirmation creates no event, so set the topic before you subscribe it. A repeat of
        one SNS message, the same ``MessageId``, is stored once. Raw message delivery must
        be off) — passed through as given, so a scheme the platform adds later works here
        unchanged; one it does not know raises ``ValidationError``. ``credential`` is sent
        as ``verification_credential``: the signing secret (under ``"stripe"``, the
        destination's signing secret, ``whsec_…``; under ``"twilio"``, a shared key made at
        Twilio for signing webhooks, as Twilio recommends, or the account's Auth Token),
        under ``"shared_secret_header"`` the header's exact value, or under ``"sns"`` the ARN
        of the topic the endpoint listens to, which is not a secret. Setting it turns
        verification on. A blank one is sent as given and the API treats it as no change:
        the current secret, if any, stays. ``content.verification.enabled`` cannot tell a
        skipped rotation from a real one, so make sure a secret read from the environment is
        not empty before passing it. It is write-only, a topic's ARN included: the API never
        returns it, and this SDK keeps no copy and never puts it in a repr or an error
        message. Pass it in the same call that moves an endpoint to or from ``"sns"``: a
        topic's ARN is never carried over to a secret scheme, or a secret to ``"sns"``, so
        a call that changes only the ``verifier`` raises ``ValidationError``. That rule binds
        only an endpoint that holds a credential: one with none is free of it. So to move off
        ``"sns"`` before the new sender's secret exists, take two calls:
        ``clear_verification()``, then ``configure_verification(verifier=...)``, with a
        ``signature_header`` too where the header rule below asks for one. From the first
        call until a ``credential`` is set, the endpoint accepts deliveries without proof;
        setting the new sender's secret is what turns verification back on. To refuse
        deliveries in the meantime, ``disable()`` the endpoint first and ``enable()`` it once
        the secret is set.
        ``signature_header`` is the header that is read: the signature's under
        ``"hmac_sha256_hex"``, the secret's under ``"shared_secret_header"``. A ``"github"``
        endpoint always reads ``X-Hub-Signature-256``, a ``"slack"`` one
        ``X-Slack-Signature``, a ``"stripe"`` one ``Stripe-Signature`` and a ``"twilio"``
        one ``X-Twilio-Signature``, so all four ignore it. An ``"sns"`` endpoint reads no
        header and ignores it too: its ``content.verification.signature_header`` is an
        empty string. Pass it in the same call that moves an endpoint to
        ``"shared_secret_header"``, or from it to ``"hmac_sha256_hex"``: a header named for
        one is never carried over to the other, so passing only the ``verifier`` raises
        ``ValidationError``. An endpoint moved from ``"shared_secret_header"`` to
        ``"github"``, ``"slack"``, ``"stripe"``, ``"twilio"`` or ``"sns"`` without a header
        has its stored header reset to ``X-Signature``, never left as the secret's, for any
        later return to ``"hmac_sha256_hex"``.

        Any viewer of the endpoint's timeline may configure it, locked timeline or not.
        Live object: the API returns the whole endpoint and this object adopts it, so
        ``content.verification`` shows the new state. With ``AsyncBaseCradle``, await this:
        ``await endpoint.configure_verification(verifier="github", credential=secret)``.
        """
        fields = {
            "verifier": verifier,
            "verification_credential": credential,
            "signature_header": signature_header,
        }
        given = {name: value for name, value in fields.items() if value is not None}
        if not given:
            raise TypeError(
                "configure_verification() needs at least one of verifier, credential or "
                "signature_header. Nothing was sent."
            )
        return self._verb(
            "PATCH", self._endpoint_path(), self._adopt, json={"webhook_endpoint": given}
        )

    def clear_verification(self) -> Any:
        """Remove the secret and turn verification off.

        Deliveries are accepted without proof again, whatever the scheme, and
        ``content.verification.enabled`` reads ``False``. The rule that a move to or
        from ``"sns"`` sets the credential in the same call binds only an endpoint that holds
        one, so this is also the first of two calls that move an endpoint off ``"sns"``
        before the new sender's secret exists; the second is
        ``configure_verification(verifier=...)``, with a ``signature_header`` where its
        header rule asks for one. Verification stays off until a ``credential`` is set; to
        refuse deliveries in the meantime, ``disable()`` the endpoint first and ``enable()``
        it once the secret is set. Live object: this object adopts the endpoint the API
        returns. With ``AsyncBaseCradle``, await this.
        """
        return self._verb(
            "PATCH",
            self._endpoint_path(),
            self._adopt,
            json={"webhook_endpoint": {"clear_credential": True}},
        )

    def _endpoint_path(self) -> str:
        return f"/webhook_endpoints/{self.content.uuid}"

    def _enablement_path(self) -> str:
        return f"{self._endpoint_path()}/enablement"

    def _adopt(self, response: dict[str, Any]) -> None:
        """Live-object update: the API returned the complete endpoint; adopt it."""
        self._data.clear()
        self._data.update(response["webhook_endpoint"])


class WebhookEventHeaders(RequestHeaders[str]):
    """One delivery's request headers: the wire's own spelling, looked up case-insensitively.

    Everything is ``RequestHeaders`` — case-folding lookup, a repr that prints names and
    never values, wire-exact reads. This name is what ``WebhookEventContent.headers``
    returns and what 0.11.0 exported, so it stays; a contact message's headers are the
    same base class. As delivered, it includes ``Content-Type`` and ``Content-Length``::

        event.content.headers["X-GitHub-Delivery"]  # GitHub's own published spelling
        event.content.headers["x-github-delivery"]  # the lowercase form
        event.content.headers["X-Github-Delivery"]  # what the wire actually carried
    """


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

    Every read path gives this same class: ``bc.webhook_events``,
    ``timeline.webhook_events``, and a ``webhook_event`` row of ``timeline.items`` alike, so
    ``item.content.headers`` folds case exactly as ``event.content.headers`` does. One
    record has one shape.
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


class _WebhookEndpointsBinding(_ItemsResourceCore[WebhookEndpoint]):
    """Webhook endpoints from every timeline you can view, newest first."""

    _path = "/webhook_endpoints"
    _plural = "webhook_endpoints"
    _singular = "webhook_endpoint"
    _model = WebhookEndpoint


class _WebhookEventsBinding(_ItemsResourceCore[WebhookEvent]):
    """Webhook events from every timeline you can view, newest first."""

    _path = "/webhook_events"
    _plural = "webhook_events"
    _singular = "webhook_event"
    _model = WebhookEvent

    def filter(self, *, timeline: Any | None = None, endpoint: Any | None = None) -> Self:
        """A new lazy resource narrowed by timeline and/or endpoint (objects or uuids)."""
        filters = self._merge_filters(timeline=timeline, endpoint=endpoint)
        return type(self)(self._client, filters=filters)


class WebhookEndpointsResource(_WebhookEndpointsBinding, ItemsResource[WebhookEndpoint]): ...


class AsyncWebhookEndpointsResource(
    _WebhookEndpointsBinding, AsyncItemsResource[WebhookEndpoint]
): ...


class WebhookEventsResource(_WebhookEventsBinding, ItemsResource[WebhookEvent]): ...


class AsyncWebhookEventsResource(_WebhookEventsBinding, AsyncItemsResource[WebhookEvent]): ...


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

    def __iter__(self) -> ListIterator[WebhookEndpoint]:
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

    def __aiter__(self) -> AsyncListIterator[WebhookEndpoint]:
        return (
            AsyncWebhookEndpointsResource(self._client)
            .filter(timeline=self._timeline_uuid)
            .__aiter__()
        )


class TimelineWebhookEvents(_NestedCreatorCore):
    """One timeline's webhook events — read-only, so iterate is all there is."""

    def __iter__(self) -> ListIterator[WebhookEvent]:
        return iter(WebhookEventsResource(self._client).filter(timeline=self._timeline_uuid))


class AsyncTimelineWebhookEvents(_NestedCreatorCore):
    """One timeline's webhook events, async — read-only."""

    def __aiter__(self) -> AsyncListIterator[WebhookEvent]:
        return (
            AsyncWebhookEventsResource(self._client)
            .filter(timeline=self._timeline_uuid)
            .__aiter__()
        )
