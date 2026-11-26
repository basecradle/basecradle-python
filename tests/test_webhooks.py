"""Webhook endpoints & events: create, enable/disable, rotate, signature verification, events."""

import json
import logging
from pathlib import Path

import httpx
import pytest

from basecradle import (
    APIConnectionError,
    ForbiddenError,
    NotFoundError,
    TimelineLockedError,
    User,
    ValidationError,
    WebhookEndpoint,
    WebhookEvent,
    WebhookVerification,
)
from tests.conftest import (
    NOVA,
    TIMELINE_UUID,
    WEBHOOK_ENDPOINT_UUID,
    WEBHOOK_EVENT_UUID,
    problem,
    timeline_payload,
    webhook_endpoint_payload,
    webhook_event_payload,
)

ROTATED_INGEST_URL = "https://basecradle.com/webhooks/019e7750-66ee-7eb8-b8a8-e882e4d6e2a9"
DELIVERY_ID = "019e7750-66ee-7d42-b8a5-4f1c9e3a7b60"  # a GitHub X-Github-Delivery value


@pytest.fixture
def timeline(bc, api):
    api.get(f"/timelines/{TIMELINE_UUID}").respond(
        200, json={"timeline": timeline_payload(), "items": []}
    )
    return bc.timelines.get(TIMELINE_UUID)


@pytest.fixture
def endpoint(bc, api, timeline):
    api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
        201, json={"webhook_endpoint": webhook_endpoint_payload()}
    )
    return timeline.webhook_endpoints.create(description="CI deploys")


class TestEndpointLifecycle:
    def test_create_disable_enable_rotate_end_to_end(self, bc, api, timeline):
        """The full lifecycle from the issue's acceptance criteria, against mocks."""
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload(enabled=True)}
        )
        enablement = api.route(
            method__in=["POST", "DELETE"],
            path=f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/enablement",
        ).mock(
            side_effect=[
                httpx.Response(
                    200, json={"webhook_endpoint": webhook_endpoint_payload(enabled=False)}
                ),
                httpx.Response(
                    200, json={"webhook_endpoint": webhook_endpoint_payload(enabled=True)}
                ),
            ]
        )
        api.post(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/rotation").respond(
            200,
            json={
                "webhook_endpoint": webhook_endpoint_payload(
                    enabled=True, ingest_url=ROTATED_INGEST_URL
                )
            },
        )

        # create
        endpoint = timeline.webhook_endpoints.create(description="CI deploys")
        assert endpoint.content.enabled is True
        original_url = endpoint.content.ingest_url

        # disable → enable
        endpoint.disable()
        assert endpoint.content.enabled is False
        endpoint.enable()
        assert endpoint.content.enabled is True
        assert enablement.call_count == 2

        # rotate: same identity, new secret URL
        endpoint.rotate()
        assert endpoint.content.uuid == WEBHOOK_ENDPOINT_UUID
        assert endpoint.content.ingest_url == ROTATED_INGEST_URL
        assert endpoint.content.ingest_url != original_url

    def test_disable_uses_delete_on_enablement(self, bc, api, endpoint):
        route = api.delete(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/enablement").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload(enabled=False)}
        )

        endpoint.disable()

        assert route.called
        assert endpoint.content.enabled is False

    def test_enable_uses_post_on_enablement(self, bc, api, endpoint):
        route = api.post(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/enablement").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload(enabled=True)}
        )

        endpoint.enable()

        assert route.called
        assert endpoint.content.enabled is True

    def test_rotate_uses_post_on_rotation(self, bc, api, endpoint):
        route = api.post(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/rotation").respond(
            200,
            json={"webhook_endpoint": webhook_endpoint_payload(ingest_url=ROTATED_INGEST_URL)},
        )

        endpoint.rotate()

        assert route.called
        assert endpoint.content.ingest_url == ROTATED_INGEST_URL

    def test_verbs_as_non_viewer_raise_forbidden(self, bc, api, endpoint):
        api.delete(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/enablement").respond(
            403, json=problem("not_a_viewer", 403)
        )

        with pytest.raises(ForbiddenError):
            endpoint.disable()


#: The signing secret the API docs' own example uses — fabricated, never a real secret.
SIGNING_SECRET = "kT9vQ2mXw7RbLp4ZsYc81NdHaUeJ3gFo"
GITHUB_VERIFICATION = {
    "enabled": True,
    "signature_header": "X-Hub-Signature-256",
    "verifier": "github",
}
OFF_VERIFICATION = {
    "enabled": False,
    "signature_header": "X-Signature",
    "verifier": "hmac_sha256_hex",
}


def sent_body(route):
    return json.loads(route.calls.last.request.content)


class TestConfigureVerification:
    """``PATCH /webhook_endpoints/{uuid}``: only the arguments given go on the wire."""

    @pytest.mark.parametrize(
        ("argument", "wire_field", "value"),
        [
            ("verifier", "verifier", "github"),
            ("credential", "verification_credential", SIGNING_SECRET),
            ("signature_header", "signature_header", "X-Hub-Signature"),
        ],
    )
    def test_each_argument_alone_sends_only_its_wire_field(
        self, bc, api, endpoint, argument, wire_field, value
    ):
        route = api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint.configure_verification(**{argument: value})

        assert sent_body(route) == {"webhook_endpoint": {wire_field: value}}

    def test_all_arguments_together(self, bc, api, endpoint):
        route = api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint.configure_verification(
            verifier="hmac_sha256_hex", credential=SIGNING_SECRET, signature_header="X-Sig"
        )

        assert sent_body(route) == {
            "webhook_endpoint": {
                "verifier": "hmac_sha256_hex",
                "verification_credential": SIGNING_SECRET,
                "signature_header": "X-Sig",
            }
        }

    def test_the_endpoint_adopts_the_verification_the_api_returns(self, bc, api, endpoint):
        api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200,
            json={"webhook_endpoint": webhook_endpoint_payload(verification=GITHUB_VERIFICATION)},
        )

        returned = endpoint.configure_verification(verifier="github", credential=SIGNING_SECRET)

        assert returned is None  # a live-object verb, like rotate(): the endpoint itself moves
        assert isinstance(endpoint.content.verification, WebhookVerification)
        assert endpoint.content.verification.enabled is True
        assert endpoint.content.verification.verifier == "github"
        assert endpoint.content.verification.signature_header == "X-Hub-Signature-256"

    def test_a_verifier_the_sdk_has_never_heard_of_is_passed_through(self, bc, api, endpoint):
        """The API's set of schemes is open; the SDK holds no list to go stale."""
        route = api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint.configure_verification(verifier="stripe")

        assert sent_body(route) == {"webhook_endpoint": {"verifier": "stripe"}}

    def test_a_shared_secret_header_endpoint_names_its_header_in_the_same_call(
        self, bc, api, endpoint
    ):
        """The API requires the header in the call that chooses this scheme: one request."""
        route = api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint.configure_verification(
            verifier="shared_secret_header",
            signature_header="X-Webhook-Secret",
            credential=SIGNING_SECRET,
        )

        assert sent_body(route) == {
            "webhook_endpoint": {
                "verifier": "shared_secret_header",
                "verification_credential": SIGNING_SECRET,
                "signature_header": "X-Webhook-Secret",
            }
        }

    @pytest.mark.parametrize(
        "scheme", ["hmac_sha256_hex", "github", "shared_secret_header", "slack"]
    )
    def test_the_docs_name_every_scheme_the_platform_has(self, scheme):
        """Four since 2026-11-25. The SDK holds no list, but its prose must not hold a short one."""
        readme = (Path(__file__).parent.parent / "README.md").read_text()

        assert f'"{scheme}"' in WebhookEndpoint.configure_verification.__doc__
        assert f'`"{scheme}"`' in readme

    def test_a_blank_credential_is_sent_as_given(self, bc, api, endpoint):
        """The API's rule is that a blank secret is no change; the SDK does not repair it."""
        route = api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint.configure_verification(credential="")

        assert sent_body(route) == {"webhook_endpoint": {"verification_credential": ""}}
        assert endpoint.content.verification.enabled is False

    def test_no_arguments_is_a_usage_error_and_sends_nothing(self, bc, api, endpoint):
        calls_before = len(api.calls)

        with pytest.raises(TypeError, match="at least one of verifier, credential"):
            endpoint.configure_verification()

        assert len(api.calls) == calls_before

    def test_an_unknown_verifier_surfaces_as_validation_error(self, bc, api, endpoint):
        api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            422,
            json=problem(
                "validation_failed", 422, errors={"verifier": ["is not included in the list"]}
            ),
        )

        with pytest.raises(ValidationError) as caught:
            endpoint.configure_verification(verifier="nope", credential=SIGNING_SECRET)

        assert caught.value.errors == {"verifier": ["is not included in the list"]}

    def test_a_non_viewer_gets_forbidden(self, bc, api, endpoint):
        api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            403, json=problem("not_a_viewer", 403)
        )

        with pytest.raises(ForbiddenError):
            endpoint.configure_verification(verifier="github")


class TestClearVerification:
    def test_sends_clear_credential_and_adopts_verification_off(self, bc, api, endpoint):
        route = api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "webhook_endpoint": webhook_endpoint_payload(
                            verification=GITHUB_VERIFICATION
                        )
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "webhook_endpoint": webhook_endpoint_payload(verification=OFF_VERIFICATION)
                    },
                ),
            ]
        )
        endpoint.configure_verification(verifier="github", credential=SIGNING_SECRET)
        assert endpoint.content.verification.enabled is True

        returned = endpoint.clear_verification()

        assert returned is None
        assert sent_body(route) == {"webhook_endpoint": {"clear_credential": True}}
        assert endpoint.content.verification.enabled is False


class TestTheSigningSecretIsWriteOnly:
    """The credential goes out on the wire once and is held, printed or logged nowhere."""

    def renderings(self, endpoint):
        return (
            repr(endpoint),
            str(endpoint),
            repr(endpoint.content),
            repr(endpoint.content.verification),
            json.dumps(endpoint.to_dict()),
            repr(vars(endpoint)),  # an attribute stashed beside the data, not in it
            repr(vars(endpoint.content)),
        )

    def test_the_endpoint_holds_no_copy_after_configuring(self, bc, api, endpoint):
        api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200,
            json={"webhook_endpoint": webhook_endpoint_payload(verification=GITHUB_VERIFICATION)},
        )

        endpoint.configure_verification(verifier="github", credential=SIGNING_SECRET)

        for rendered in self.renderings(endpoint):
            assert SIGNING_SECRET not in rendered

    def test_no_log_record_from_the_request_carries_the_secret(self, bc, api, endpoint, caplog):
        """Every logger, at DEBUG, while the secret is sent: httpx's own request line too."""
        api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200,
            json={"webhook_endpoint": webhook_endpoint_payload(verification=GITHUB_VERIFICATION)},
        )

        with caplog.at_level(logging.DEBUG):
            endpoint.configure_verification(credential=SIGNING_SECRET)
            logging.getLogger("basecradle.tests").debug("configured %r %s", endpoint, endpoint)

        # The floor: httpx really logged this request, so the absence below is measured.
        assert any(
            record.name == "httpx" and "PATCH" in record.getMessage() for record in caplog.records
        )
        assert SIGNING_SECRET not in caplog.text

    def test_an_api_error_carries_no_secret(self, bc, api, endpoint):
        api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            422,
            json=problem("validation_failed", 422, errors={"verifier": ["is not included"]}),
        )

        with pytest.raises(ValidationError) as caught:
            endpoint.configure_verification(verifier="nope", credential=SIGNING_SECRET)

        error = caught.value
        for rendered in (str(error), repr(error), repr(vars(error))):
            assert SIGNING_SECRET not in rendered

    def test_a_connection_errors_message_and_repr_carry_no_secret(self, bc, api, endpoint):
        """Its ``__cause__`` is httpx's own error, which holds the request, as for the token."""
        api.patch(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").mock(
            side_effect=httpx.ConnectError("refused")
        )

        with pytest.raises(APIConnectionError) as caught:
            endpoint.configure_verification(credential=SIGNING_SECRET)

        error = caught.value
        for rendered in (str(error), repr(error), repr(vars(error))):
            assert SIGNING_SECRET not in rendered


class TestEndpointCreate:
    def test_create_sends_rails_nested_body(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint = timeline.webhook_endpoints.create(description="CI deploys")

        assert json.loads(route.calls.last.request.read()) == {
            "webhook_endpoint": {"description": "CI deploys"}
        }
        assert isinstance(endpoint, WebhookEndpoint)
        assert endpoint.content.description == "CI deploys"
        assert endpoint.content.ingest_url.startswith("https://basecradle.com/webhooks/")

    def test_create_with_idempotency_key_sends_header(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        timeline.webhook_endpoints.create(description="CI deploys", idempotency_key="key-3")

        assert route.calls.last.request.headers["Idempotency-Key"] == "key-3"

    def test_create_without_idempotency_key_sends_no_header(self, bc, api, timeline):
        route = api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        timeline.webhook_endpoints.create(description="CI deploys")

        assert "Idempotency-Key" not in route.calls.last.request.headers

    @pytest.mark.parametrize("key", ["", " ", "\t", " \t\n "])
    def test_create_with_blank_idempotency_key_sends_no_header(self, bc, api, timeline, key):
        route = api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        timeline.webhook_endpoints.create(description="CI deploys", idempotency_key=key)

        assert "Idempotency-Key" not in route.calls.last.request.headers

    @pytest.mark.parametrize("key", [" key-1 ", "\x00", "clé-1"])
    def test_create_with_unsendable_idempotency_key_raises_before_any_request(
        self, bc, api, timeline, key
    ):
        with pytest.raises(ValueError, match="idempotency_key"):
            timeline.webhook_endpoints.create(description="CI deploys", idempotency_key=key)

    def test_verification_block_is_typed(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint = timeline.webhook_endpoints.create(description="CI deploys")

        assert isinstance(endpoint.content.verification, WebhookVerification)
        assert endpoint.content.verification.enabled is False
        assert endpoint.content.verification.signature_header == "X-Signature"
        assert endpoint.content.verification.verifier == "hmac_sha256_hex"

    def test_create_on_locked_timeline_raises(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            403, json=problem("timeline_locked", 403)
        )

        with pytest.raises(TimelineLockedError):
            timeline.webhook_endpoints.create(description="CI deploys")

    def test_create_blank_description_raises(self, bc, api, timeline):
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            422,
            json=problem("validation_failed", 422, errors={"description": ["can't be blank"]}),
        )

        with pytest.raises(ValidationError) as exc_info:
            timeline.webhook_endpoints.create(description="")

        assert exc_info.value.errors == {"description": ["can't be blank"]}

    def test_endpoint_carries_its_author(self, bc, api, timeline):
        """An endpoint is authored: ``user`` is the peer who created it, nested-actor form."""
        api.post(f"/timelines/{TIMELINE_UUID}/webhook_endpoints").respond(
            201, json={"webhook_endpoint": webhook_endpoint_payload(user=NOVA)}
        )

        endpoint = timeline.webhook_endpoints.create(description="CI deploys")

        assert isinstance(endpoint.user, User)
        assert endpoint.user.handle == "nova"
        assert endpoint.user.kind == "ai"
        assert endpoint.updated_at == "2026-01-02T00:00:00.000Z"


class TestEndpointsResource:
    def test_iteration_paginates(self, bc, api):
        api.get("/webhook_endpoints").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "webhook_endpoints": [webhook_endpoint_payload()],
                        "next_cursor": "019e7750-66ee-7611-8e63-26d6c2a2c6f5",
                    },
                ),
                httpx.Response(
                    200,
                    json={"webhook_endpoints": [webhook_endpoint_payload()], "next_cursor": None},
                ),
            ]
        )

        endpoints = list(bc.webhook_endpoints)

        assert len(endpoints) == 2
        assert all(isinstance(e, WebhookEndpoint) for e in endpoints)

    def test_get(self, bc, api):
        api.get(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            200, json={"webhook_endpoint": webhook_endpoint_payload()}
        )

        endpoint = bc.webhook_endpoints.get(WEBHOOK_ENDPOINT_UUID)

        assert isinstance(endpoint, WebhookEndpoint)
        assert endpoint.content.uuid == WEBHOOK_ENDPOINT_UUID

    def test_get_unknown_uuid_raises(self, bc, api):
        api.get(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}").respond(
            404, json=problem("not_found", 404)
        )

        with pytest.raises(NotFoundError):
            bc.webhook_endpoints.get(WEBHOOK_ENDPOINT_UUID)

    def test_filter_by_timeline(self, bc, api):
        route = api.get("/webhook_endpoints", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"webhook_endpoints": [webhook_endpoint_payload()], "next_cursor": None}
        )

        list(bc.webhook_endpoints.filter(timeline=TIMELINE_UUID))

        assert route.called

    def test_nested_iteration(self, bc, api, timeline):
        route = api.get("/webhook_endpoints", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"webhook_endpoints": [webhook_endpoint_payload()], "next_cursor": None}
        )

        list(timeline.webhook_endpoints)

        assert route.called


class TestEventsResource:
    def test_iteration_paginates(self, bc, api):
        api.get("/webhook_events").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "webhook_events": [webhook_event_payload()],
                        "next_cursor": "019e7750-66ee-7611-8e63-26d6c2a2c6f5",
                    },
                ),
                httpx.Response(
                    200, json={"webhook_events": [webhook_event_payload()], "next_cursor": None}
                ),
            ]
        )

        events = list(bc.webhook_events)

        assert len(events) == 2
        assert all(isinstance(e, WebhookEvent) for e in events)

    def test_get_typed_content(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload()}
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)

        assert event.content.content_type == "application/json"
        assert event.content.payload == '{"status":"ok"}'
        assert event.content.headers == {
            "Host": "basecradle.com",
            "User-Agent": "Example-Hooks/1.0",
            "Content-Type": "application/json",
            "Content-Length": "15",
            "X-Example-Event": "ping",
        }
        assert event.content.ingest_token_at_receipt == "019e7750-66ee-705a-803c-b25c5ee9b1f3"
        assert event.updated_at == "2026-01-02T00:00:00.000Z"

    @pytest.mark.parametrize(
        "spelling",
        [
            "X-GitHub-Delivery",  # GitHub's own published spelling
            "x-github-delivery",  # the lowercase form HTTP/2 senders emit
            "X-Github-Delivery",  # what the wire actually carried
            "X-GITHUB-DELIVERY",  # any other casing
        ],
    )
    def test_header_lookup_folds_case(self, bc, api, spelling):
        """Header names are case-insensitive by RFC, so every casing reads the same header.

        The platform rewrites names to canonical Title-Case per segment and its docs tell
        consumers to look them up case-insensitively — so the vendor spelling a caller reads
        out of GitHub's own documentation resolves, rather than raising.
        """
        headers = self._delivered_headers(bc, api, {"X-Github-Delivery": DELIVERY_ID})

        assert headers[spelling] == DELIVERY_ID
        assert headers.get(spelling) == DELIVERY_ID
        assert spelling in headers

    def test_header_names_iterate_in_the_wires_own_spelling(self, bc, api):
        """Only *lookup* folds case: nothing is renamed, so reads still match the wire.

        The object is a plain ``dict`` of the pairs the API returned — iteration, ``keys()``
        and ``==`` read the platform's canonical spelling, which is what a reader
        cross-referencing ``api.md`` sees.
        """
        wire = {"X-Github-Delivery": DELIVERY_ID, "Content-Length": "2"}
        headers = self._delivered_headers(bc, api, wire)

        assert isinstance(headers, dict)
        assert headers == wire
        assert list(headers) == ["X-Github-Delivery", "Content-Length"]
        assert sorted(headers.keys()) == ["Content-Length", "X-Github-Delivery"]

        # A copy is another headers object, not a plain dict that lost the case folding.
        assert headers.copy()["X-GitHub-Delivery"] == wire["X-Github-Delivery"]

    def test_a_non_string_key_breaks_none_of_the_readers(self, bc, api):
        """The wire cannot deliver one — JSON keys are strings — but the ``dict`` mutators
        are deliberately left open, so a caller can put one here.

        Three readers walk the keys, and a bare ``sorted`` or ``.lower()`` over them turns
        each into the wrong exception: ``__repr__`` into a ``TypeError`` that makes
        ``logging`` discard the whole record (the very thing a redacting repr must never
        do), ``__getitem__``'s "headers present" message into that same ``TypeError``
        instead of the ``KeyError`` it owes, and every later lookup into an
        ``AttributeError`` where ``_wire_name`` promises a plain "absent".
        """
        headers = self._delivered_headers(bc, api, {"X-Github-Delivery": DELIVERY_ID})
        headers[42] = "not a header name"

        # The repr shows everything the object holds -- hiding a key would be a lie.
        assert repr(headers) == "<WebhookEventHeaders [42, 'X-Github-Delivery']>"
        assert headers["x-github-delivery"] == DELIVERY_ID
        assert headers.get("X-Nothing") is None
        with pytest.raises(KeyError, match="No 'X-Nothing' header"):
            headers["X-Nothing"]

        # Lookup reports it absent, because it is not a header name -- consistently, and
        # with a message that does not then list it among the headers that arrived.
        assert 42 not in headers
        assert headers.get(42) is None
        with pytest.raises(KeyError, match=r"Headers present: \['X-Github-Delivery'\]"):
            headers[42]

    def test_a_header_that_was_not_delivered_is_absent_rather_than_none(self, bc, api):
        """No casing of it arrived, so it is missing — the SDK never invents a ``None``.

        ``ApiObject`` refuses a silent ``None`` for a field the API did not return; a header
        the sender did not send is refused the same way, and the error names what did arrive.
        """
        headers = self._delivered_headers(bc, api, {"X-Github-Delivery": DELIVERY_ID})

        with pytest.raises(KeyError, match="Stripe-Signature") as absent:
            headers["Stripe-Signature"]
        assert "X-Github-Delivery" in str(absent.value)  # names the headers that did arrive

        assert "Stripe-Signature" not in headers
        assert headers.get("Stripe-Signature") is None
        assert headers.get("Stripe-Signature", "unsigned") == "unsigned"

    def test_headers_the_api_omitted_raise_the_models_missing_field_error(self, bc, api):
        """Presenting headers richer than their wire type must not lose the absence error."""
        event_payload = webhook_event_payload()
        del event_payload["content"]["headers"]
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": event_payload}
        )

        content = bc.webhook_events.get(WEBHOOK_EVENT_UUID).content

        with pytest.raises(AttributeError, match="did not return 'headers'"):
            content.headers

    def test_a_name_that_is_not_a_string_is_absent_rather_than_a_crash(self, bc, api):
        """Nothing but a string can be a header name, so one reads as missing, not as a bug.

        Folding case means calling ``lower()``; an unguarded one would turn ``headers[42]``
        into an ``AttributeError`` about ``int``, where a mapping owes a ``KeyError``.
        """
        headers = self._delivered_headers(bc, api, {"X-Github-Delivery": DELIVERY_ID})

        with pytest.raises(KeyError):
            headers[42]
        assert 42 not in headers
        assert headers.get(None) is None

    def test_headers_the_api_sends_as_null_come_back_as_the_wire_sent_them(self, bc, api):
        """Not a shape ``/webhook_events`` returns — but the SDK never invents one either.

        ``headers`` is required and non-nullable on an event, so this cannot happen today.
        The API is additive-only and new response forms land without SDK changes, so the
        richer presentation defers to the wire for anything that is not an object, exactly
        as ``ApiObject`` does for a nested model.
        """
        headers = self._delivered_headers(bc, api, None)

        assert headers is None

    @staticmethod
    def _delivered_headers(bc, api, wire_headers):
        """The headers of one fetched event, with ``wire_headers`` as what the delivery sent."""
        event_payload = webhook_event_payload()
        event_payload["content"]["headers"] = wire_headers
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": event_payload}
        )
        return bc.webhook_events.get(WEBHOOK_EVENT_UUID).content.headers

    @pytest.mark.parametrize("verified", [False, True])
    def test_verified_at_receipt_is_the_deliverys_own_historical_fact(self, bc, api, verified):
        """Whether *this* delivery's signature checked out when it arrived.

        It is fixed at receipt, unlike the embedded endpoint's ``verification`` block,
        which reports the endpoint's requirements *now*.
        """
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload(verified_at_receipt=verified)}
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)

        assert event.content.verified_at_receipt is verified
        assert event.webhook_endpoint.content.verification.enabled is False  # current, not past

    def test_event_references_its_timeline_and_embeds_its_endpoint(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload()}
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)

        assert event.timeline.uuid == TIMELINE_UUID
        assert event.webhook_endpoint.content.uuid == WEBHOOK_ENDPOINT_UUID

    def test_events_have_no_user(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload()}
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)

        with pytest.raises(AttributeError):
            event.user

    def test_get_not_a_viewer_raises(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            403, json=problem("not_a_viewer", 403)
        )

        with pytest.raises(ForbiddenError):
            bc.webhook_events.get(WEBHOOK_EVENT_UUID)

    def test_filter_by_endpoint_object(self, bc, api, endpoint):
        route = api.get("/webhook_events", params={"endpoint": WEBHOOK_ENDPOINT_UUID}).respond(
            200, json={"webhook_events": [webhook_event_payload()], "next_cursor": None}
        )

        list(bc.webhook_events.filter(endpoint=endpoint))

        assert route.called

    def test_filter_by_endpoint_uuid_string(self, bc, api):
        route = api.get("/webhook_events", params={"endpoint": WEBHOOK_ENDPOINT_UUID}).respond(
            200, json={"webhook_events": [], "next_cursor": None}
        )

        list(bc.webhook_events.filter(endpoint=WEBHOOK_ENDPOINT_UUID))

        assert route.called

    def test_timeline_and_endpoint_filters_compose(self, bc, api):
        route = api.get(
            "/webhook_events",
            params={"timeline": TIMELINE_UUID, "endpoint": WEBHOOK_ENDPOINT_UUID},
        ).respond(200, json={"webhook_events": [], "next_cursor": None})

        list(bc.webhook_events.filter(timeline=TIMELINE_UUID, endpoint=WEBHOOK_ENDPOINT_UUID))

        assert route.called

    def test_nested_iteration(self, bc, api, timeline):
        route = api.get("/webhook_events", params={"timeline": TIMELINE_UUID}).respond(
            200, json={"webhook_events": [webhook_event_payload()], "next_cursor": None}
        )

        events = list(timeline.webhook_events)

        assert route.called
        assert all(isinstance(e, WebhookEvent) for e in events)


class TestEventEndpointEmbed:
    """An event embeds its endpoint in full — the API's one exception to the direction rule.

    A reader of an event almost always wants the endpoint's *current* ingest URL and state
    next, so the whole endpoint rides along rather than a uuid to dereference.
    """

    def test_embedded_endpoint_is_a_full_endpoint_object(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload()}
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)

        assert isinstance(event.webhook_endpoint, WebhookEndpoint)
        assert event.webhook_endpoint.content.uuid == WEBHOOK_ENDPOINT_UUID
        assert event.webhook_endpoint.content.description == "CI deploys"
        assert isinstance(event.webhook_endpoint.content.verification, WebhookVerification)
        assert event.webhook_endpoint.timeline.uuid == TIMELINE_UUID

    def test_the_embedded_endpoint_carries_its_author(self, bc, api):
        """An event has no author of its own; the endpoint it arrived on does."""
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload()}
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)

        assert event.webhook_endpoint.user.handle == "john"

    def test_endpoint_verbs_are_reachable_from_the_embed(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload()}
        )
        rotation = api.post(f"/webhook_endpoints/{WEBHOOK_ENDPOINT_UUID}/rotation").respond(
            200,
            json={"webhook_endpoint": webhook_endpoint_payload(ingest_url=ROTATED_INGEST_URL)},
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)
        event.webhook_endpoint.rotate()

        assert rotation.called
        assert event.webhook_endpoint.content.ingest_url == ROTATED_INGEST_URL

    def test_the_embedded_endpoint_is_a_filter_value(self, bc, api):
        api.get(f"/webhook_events/{WEBHOOK_EVENT_UUID}").respond(
            200, json={"webhook_event": webhook_event_payload()}
        )
        route = api.get("/webhook_events", params={"endpoint": WEBHOOK_ENDPOINT_UUID}).respond(
            200, json={"webhook_events": [], "next_cursor": None}
        )

        event = bc.webhook_events.get(WEBHOOK_EVENT_UUID)
        list(bc.webhook_events.filter(endpoint=event.webhook_endpoint))

        assert route.called
