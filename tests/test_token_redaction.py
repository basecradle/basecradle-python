"""Nothing generic may emit a credential (#242, #246).

The class of bug: *a generic serializer or representation of an object holding a secret
emits the secret.* The client holds a ``bc_uat_`` token; every resource and every model
holds the client. So every surface that renders, walks, or serializes one of those three
has to be measured, and what was measured has to stay measured — which is what this module
is for.

The credential is not always **ours**. ``WebhookEventHeaders`` holds no client and no
``bc_uat_`` token, but an inbound delivery's headers carry whatever the *sender* put in
them, and a sender authenticating to an ingest URL puts its own secret there (#246). The
party harmed by printing those never agreed to our logging, which makes it the same bug
and not a lesser one — so the charter here is any secret reachable through a generic
surface, whoever it belongs to.

Each test asserts on the **absence of the token string**, not on a redaction spelling, so
a future refactor that reintroduces the credential by another route still fails here.
``test_every_resource_...`` sweeps the resources by discovery rather than by name, so a
resource added later cannot quietly skip the guarantee.
"""

import copy
import io
import json
import logging
import pickle
import pprint

import pytest

import basecradle
from basecradle import (
    APIConnectionError,
    ApiObject,
    AsyncBaseCradle,
    AsyncItemsResource,
    BaseCradle,
    BaseCradleError,
    ItemsResource,
    Timeline,
    WebhookEndpoint,
    WebhookEvent,
)
from tests.conftest import (
    BASE_URL,
    FAKE_TOKEN,
    INGEST_URL,
    problem,
    timeline_payload,
    webhook_endpoint_payload,
    webhook_event_payload,
)

#: A sender's own secrets, fabricated. Not this client's credential and never the bearer
#: token: these are the headers an inbound delivery arrived with, so the party exposed by
#: printing them is the **sender** -- who never agreed to our logging (#246).
SENDER_SECRETS = {
    "Authorization": "Bearer sender-secret-4tPq9wKzR2mXbN7v",
    "X-Api-Key": "sk-sender-8jHdL3cQyW1nZuEa",
}


def delivery_with_sender_secrets(client):
    """One webhook event whose delivery carried a sender's ``Authorization``/``X-Api-Key``."""
    payload = webhook_event_payload()
    payload["content"]["headers"] = {**payload["content"]["headers"], **SENDER_SECRETS}
    return WebhookEvent(payload, client=client)


# Every attribute on a client that is a resource, and every nested resource on a Timeline.
CLIENT_RESOURCES = (
    "timelines",
    "messages",
    "assets",
    "tasks",
    "webhook_endpoints",
    "webhook_events",
    "sessions",
    "users",
    "contact_messages",
    "notes",
)
TIMELINE_RESOURCES = ("messages", "assets", "tasks", "webhook_endpoints", "webhook_events")


@pytest.fixture
def log_stream():
    """A DEBUG logger writing into a ``StringIO``, restored to exactly how it was found.

    One harness for the three tests here that capture log output: there were two
    hand-rolled copies of it, and #246's test was about to be a third. A logger is a
    process-global singleton, so the level and ``propagate`` are put back, the handler is
    removed *and closed*, and while the fixture is live nothing propagates the fabricated
    secrets to a handler it did not install.

    The "the handler really ran" floor stays in the **test bodies**, deliberately. An
    absence assertion over an empty string passes while testing nothing, and that is a
    failure *of that test, at the line that went vacuous*. Asserted on teardown instead it
    would fire on every skip and every unrelated failure in a test using this fixture, add
    a second and misdirecting error beside the real one, and report the vacuous test
    itself as **passed** with an error attached — which is worse than the duplication it
    would save. A test whose assertion is *positive* needs no floor at all: an empty
    stream already fails it.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    log = logging.getLogger("basecradle.tests.capture")
    level, propagate = log.level, log.propagate
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.addHandler(handler)
    try:
        yield log, stream
    finally:
        log.removeHandler(handler)
        handler.close()
        log.setLevel(level)
        log.propagate = propagate


@pytest.fixture
def clients():
    """One of each client, on the same fabricated token. Neither makes a request."""
    sync_client = BaseCradle(token=FAKE_TOKEN)
    async_client = AsyncBaseCradle(token=FAKE_TOKEN)
    yield sync_client, async_client
    sync_client.close()
    # The async pool needs a loop to close and nothing was ever opened: no request was
    # made, so there is no connection to leak. Dropping the reference is the whole cleanup.


def every_resource(client):
    """Every resource reachable from a client: the ten on it, two filtered, the nested five."""
    timeline = Timeline(timeline_payload(), client=client)
    return [
        *(getattr(client, name) for name in CLIENT_RESOURCES),
        client.messages.filter(timeline=timeline),
        client.contact_messages.filter(status="received"),
        *(getattr(timeline, name) for name in TIMELINE_RESOURCES),
    ]


class TestClientRepresentation:
    """``repr``/``str``/``format``/``pprint`` of a client: the credential named, withheld."""

    def test_repr_shows_base_url_and_a_fixed_redaction(self, clients):
        for client in clients:
            assert repr(client) == (
                f"<{type(client).__name__} base_url='{BASE_URL}' token=[REDACTED]>"
            )

    def test_every_stringification_routes_through_repr(self, clients):
        """``str``/``format``/f-string/``pprint`` all inherit ``__repr__`` — pinned so a
        later ``__str__`` or ``__format__`` cannot quietly open a second, unredacted path.

        That the token is absent from ``repr`` is pinned in ``test_client`` and
        ``test_async_client``; what is asserted here is that every other spelling agrees
        with it, which is the part a new dunder could break.
        """
        for client in clients:
            expected = repr(client)
            for rendered in (
                str(client),
                f"{client}",
                f"{client!r}",
                f"{client!s}",
                format(client),
                pprint.pformat(client),
            ):
                assert rendered == expected
                assert FAKE_TOKEN not in rendered


class TestAttributeWalks:
    """``vars()``/``__dict__`` — what every generic serializer and crash reporter walks."""

    def test_the_token_is_not_an_instance_attribute(self, clients):
        for client in clients:
            assert "token" not in vars(client)
            assert vars(client) is client.__dict__
            assert FAKE_TOKEN not in repr(vars(client))

    def test_no_attribute_value_is_the_token(self, clients):
        """Not just the key: no *value* in the instance dict is the credential either."""
        for client in clients:
            assert [name for name, value in vars(client).items() if value == FAKE_TOKEN] == []

    def test_pretty_printing_the_instance_dict_is_clean(self, clients):
        for client in clients:
            assert FAKE_TOKEN not in pprint.pformat(vars(client))

    def test_the_token_is_in_a_slot_not_the_instance_dict(self, clients):
        """Where the credential actually lives, asserted directly rather than by absence."""
        for client in clients:
            assert "_token" in type(client).__mro__[-2].__slots__
            assert "_token" not in vars(client)
            assert client._token == FAKE_TOKEN  # readable by name, absent from __dict__

    def test_no_module_global_holds_a_token(self):
        """The first fix for this used a module-level store; it concentrated every live
        client's credential in one walkable object. A slot has no such object — pinned so
        a future refactor cannot reintroduce one.
        """
        import basecradle._client
        import basecradle._resources

        for module in (basecradle._client, basecradle._resources):
            for name, value in vars(module).items():
                assert FAKE_TOKEN not in repr(value), f"{module.__name__}.{name}"

    def test_a_crash_reporter_expanding_frame_locals_is_clean(self, clients):
        """Sentry, ``rich``, ``cgitb`` and IPython's ``%debug`` all render locals via ``__dict__``.

        The client is passed as an argument down three frames, so it is in every frame's
        locals — exactly the shape of a real traceback through SDK calling code.
        """

        def leaf(client, attempt):
            raise RuntimeError("boom")

        def middle(client):
            leaf(client, attempt=1)

        for client in clients:
            with pytest.raises(RuntimeError) as caught:
                middle(client)

            traceback_ = caught.value.__traceback__
            frames = []
            while traceback_ is not None:
                frames.append(traceback_.tb_frame)
                traceback_ = traceback_.tb_next

            expanded = "\n".join(
                repr(
                    {
                        name: vars(value) if hasattr(value, "__dict__") else value
                        for name, value in frame.f_locals.items()
                    }
                )
                for frame in frames
            )
            assert FAKE_TOKEN not in expanded


class TestSerializationRefusal:
    """The pickle protocol is the one generic serialization hook, so it refuses by name."""

    def test_the_client_refuses_every_pickle_protocol(self, clients):
        for client in clients:
            for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
                with pytest.raises(BaseCradleError) as caught:
                    pickle.dumps(client, protocol)
                assert FAKE_TOKEN not in str(caught.value)

    def test_the_client_refuses_copy_and_deepcopy(self, clients):
        """``copy.copy(client)`` used to hand back a second live client on the credential."""
        for client in clients:
            for clone in (copy.copy, copy.deepcopy):
                with pytest.raises(BaseCradleError):
                    clone(client)

    def test_the_client_refuses_reduce_directly(self, clients):
        """``__reduce__`` used to return the whole ``__dict__``, token first."""
        for client in clients:
            with pytest.raises(BaseCradleError):
                client.__reduce__()
            for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
                with pytest.raises(BaseCradleError):
                    client.__reduce_ex__(protocol)

    def test_the_client_refusal_names_the_credential_risk(self, clients):
        for client in clients:
            with pytest.raises(BaseCradleError) as caught:
                pickle.dumps(client)
            message = str(caught.value)
            assert "cannot be serialized" in message
            assert "bearer token" in message
            assert f"{type(client).__name__}(token=...)" in message

    def test_the_client_is_not_json_serializable(self, clients):
        for client in clients:
            with pytest.raises(TypeError):
                json.dumps(client)


class TestResourceRepresentation:
    def test_a_list_resource_shows_its_path_and_filters(self, clients):
        sync_client, async_client = clients
        assert repr(sync_client.messages) == "<MessagesResource path='/messages' filters={}>"
        assert repr(async_client.messages) == "<AsyncMessagesResource path='/messages' filters={}>"

    def test_a_filtered_resource_shows_what_narrowed_it(self, clients):
        """The first thing you want from a lazy list's repr: everything, or narrowed?"""
        sync_client, async_client = clients
        uuid = timeline_payload()["uuid"]
        assert repr(sync_client.messages.filter(timeline=uuid)) == (
            f"<MessagesResource path='/messages' filters={{'timeline': '{uuid}'}}>"
        )
        assert repr(async_client.messages.filter(timeline=uuid)) == (
            f"<AsyncMessagesResource path='/messages' filters={{'timeline': '{uuid}'}}>"
        )

    def test_a_resource_holding_only_the_client_shows_just_its_name(self, clients):
        sync_client, _ = clients
        assert repr(sync_client.timelines) == "<TimelinesResource>"
        assert repr(sync_client.sessions) == "<SessionsResource>"
        assert repr(sync_client.users) == "<UsersResource>"

    def test_a_nested_creator_shows_its_timeline(self, clients):
        uuid = timeline_payload()["uuid"]
        for client in clients:
            timeline = Timeline(timeline_payload(), client=client)
            assert repr(timeline.messages).endswith(f"timeline='{uuid}'>")

    def test_the_exported_unbound_resources_have_a_repr_that_does_not_raise(self, clients):
        """``ItemsResource``/``AsyncItemsResource`` are in ``basecradle.__all__`` but carry
        no ``_path`` — there it is a bare annotation, supplied only by the bindings.

        A repr that raises is worse than a dull one: ``logging`` discards the **entire**
        record when a ``%r`` argument blows up, so a repr added to keep credentials out of
        the logs would be taking the logs down instead.
        """
        sync_client, async_client = clients
        for resource in (ItemsResource(sync_client), AsyncItemsResource(async_client)):
            rendered = repr(resource)  # must not raise
            assert rendered == f"<{type(resource).__name__} filters={{}}>"
            assert FAKE_TOKEN not in rendered

    def test_logging_an_unbound_resource_keeps_the_log_record(self, clients, log_stream):
        """The failure mode above, asserted end to end: the record must still be written.

        No "the handler ran" floor here, and none is owed: this assertion is positive, so
        an empty stream fails it already.
        """
        log, stream = log_stream
        log.debug("resource=%r", ItemsResource(clients[0]))
        assert "resource=<ItemsResource filters={}>" in stream.getvalue()

    def test_no_resource_repr_carries_a_memory_address(self, clients):
        """The default ``object.__repr__`` is both useless and unreviewable — replaced."""
        for client in clients:
            for resource in every_resource(client):
                assert " object at 0x" not in repr(resource)


class TestEveryResourceIsCovered:
    """Swept by discovery, so a resource added later cannot skip the guarantee."""

    def test_every_resource_redacts_and_refuses(self, clients):
        for client in clients:
            for resource in every_resource(client):
                rendered = repr(resource)
                assert FAKE_TOKEN not in rendered, rendered
                assert FAKE_TOKEN not in repr(vars(resource)), rendered

                with pytest.raises(BaseCradleError) as caught:
                    pickle.dumps(resource)
                assert "cannot be serialized" in str(caught.value)
                assert FAKE_TOKEN not in str(caught.value)

                for clone in (copy.copy, copy.deepcopy):
                    with pytest.raises(BaseCradleError):
                        clone(resource)

    def test_the_resource_refusal_points_at_explicit_iteration(self, clients):
        sync_client, _ = clients
        with pytest.raises(BaseCradleError) as caught:
            pickle.dumps(sync_client.messages)
        message = str(caught.value)
        assert "MessagesResource cannot be serialized" in message
        assert "fetched lazily" in message
        assert "list(...)" in message

    def test_client_resource_list_is_complete(self, clients):
        """Guards the sweep itself: an unlisted resource would silently go unswept."""
        for client in clients:
            discovered = {
                name
                for name, value in vars(client).items()
                if not name.startswith("_") and "Resource" in type(value).__name__
            }
            assert discovered == set(CLIENT_RESOURCES)


class TestIndirectRefusal:
    """``deepcopy`` of a record or resource recurses into the client and lands on its
    refusal, so that message has to make sense to someone who copied a record."""

    def test_deepcopying_an_attached_record_refuses_and_says_something_true(self, clients):
        for client in clients:
            record = Timeline(timeline_payload(), client=client)
            with pytest.raises(BaseCradleError) as caught:
                copy.deepcopy(record)
            message = str(caught.value)
            assert "Nothing holding a client can be serialized" in message
            assert "copying a resource or a record reaches this too" in message
            assert FAKE_TOKEN not in message

    def test_shallow_copying_a_record_still_works(self, clients):
        """It never touches the client, so it was never a way out with the credential."""
        for client in clients:
            record = Timeline(timeline_payload(), client=client)
            clone = copy.copy(record)
            assert clone == record
            assert FAKE_TOKEN not in repr(clone)

    def test_a_caller_registered_reducer_bypasses_the_refusal(self, clients):
        """A documented **limit**, pinned so the docstrings cannot drift back to claiming it.

        ``copyreg.dispatch_table`` and ``Pickler.reducer_override`` are consulted *before*
        ``__reduce__``, so a caller who registers its own reducer for a client is not
        stopped by ours. That is a caller overriding deliberately, not a hole — but an
        earlier draft of this change claimed ``copyreg`` was covered, and it is not.
        """
        import copyreg

        sync_client, _ = clients
        copyreg.pickle(BaseCradle, lambda client: (dict, ({"base_url": client.base_url},)))
        try:
            assert copy.copy(sync_client) == {"base_url": BASE_URL}
        finally:
            del copyreg.dispatch_table[BaseCradle]
        # and with the registration gone, the refusal is back
        with pytest.raises(BaseCradleError):
            copy.copy(sync_client)


class TestTheWorstPath:
    """``json.dump`` to a *stream* wrote the token out and only then raised (#242).

    The ``ValueError: Circular reference detected`` that stops ``json.dumps`` discards its
    partial result; ``json.dump`` to a file, socket or log stream has already flushed it.
    What is closed here is the ``default=vars`` idiom — see
    ``test_a_deep_enough_walker_still_reaches_httpx`` for the limit of that guarantee.
    """

    def test_json_dump_with_default_vars_writes_no_credential(self, clients):
        for client in clients:
            subjects = [
                client,
                client.messages,
                ApiObject(timeline_payload(), client=client),
                Timeline(timeline_payload(), client=client),
            ]
            for subject in subjects:
                stream = io.StringIO()
                # Always raises -- the point is what reached the stream first.
                with pytest.raises((ValueError, TypeError, BaseCradleError)):
                    json.dump(subject, stream, default=vars)
                assert FAKE_TOKEN not in stream.getvalue(), type(subject).__name__

    def test_json_dumps_of_an_attached_model_never_returns(self, clients):
        for client in clients:
            model = ApiObject(timeline_payload(), client=client)
            with pytest.raises(TypeError):
                json.dumps(model)

    def test_a_deep_enough_walker_still_reaches_httpx(self, clients):
        """The documented **limit** of this hardening, pinned so it stays documented.

        A client must hold its credential somewhere to authenticate, and it hands it to
        ``httpx`` as a default header. So a serializer that recursively walks *every*
        attribute of every object it reaches — tolerating cycles, following private names —
        arrives at ``httpx``'s own header storage. Measured: depth 8 from a client, 10 from
        a resource or a record. ``httpx`` redacts ``authorization`` in its own
        ``Headers.__repr__``; only a walker that bypasses reprs gets the value.

        This is not the #242 class of bug, which is a *generic* serializer emitting the
        credential unasked — those are closed above. It is the irreducible floor: an
        exhaustive attribute walk finds any secret any object holds. Closing it would mean
        authenticating per request instead of once per client, which is a design change
        and a founder/capital call, not a local fix.

        If this test ever fails, the floor moved — update the CHANGELOG's stated limit
        rather than deleting the test.
        """

        def walk(obj, depth, seen, limit):
            if depth > limit or id(obj) in seen:
                return "..."
            seen = seen | {id(obj)}
            if isinstance(obj, (str, bytes, int, float, bool, type(None))):
                return repr(obj)
            if isinstance(obj, dict):
                return "".join(
                    walk(key, depth + 1, seen, limit) + walk(value, depth + 1, seen, limit)
                    for key, value in obj.items()
                )
            if isinstance(obj, (list, tuple, set)):
                return "".join(walk(value, depth + 1, seen, limit) for value in obj)
            attributes = getattr(obj, "__dict__", None)
            return walk(attributes, depth + 1, seen, limit) if attributes else repr(obj)

        for client in clients:
            assert FAKE_TOKEN not in walk(client, 0, set(), 7)
            assert FAKE_TOKEN in walk(client, 0, set(), 8)
            assert FAKE_TOKEN not in walk(client.messages, 0, set(), 9)
            assert FAKE_TOKEN in walk(client.messages, 0, set(), 10)


class TestModels:
    """An ``ApiObject`` holds ``_client``, so it is measured for the same class of leak."""

    def test_repr_shows_field_names_never_values(self, clients):
        """The ingest URL is a credential too, and a keys-only repr already withheld it."""
        for client in clients:
            endpoint = WebhookEndpoint(webhook_endpoint_payload(), client=client)
            rendered = repr(endpoint)
            assert rendered == (
                "<WebhookEndpoint ['content', 'created_at', 'timeline', 'type', "
                "'updated_at', 'user']>"
            )
            assert INGEST_URL not in rendered
            assert FAKE_TOKEN not in rendered

    def test_nested_content_repr_withholds_the_ingest_url(self, clients):
        for client in clients:
            endpoint = WebhookEndpoint(webhook_endpoint_payload(), client=client)
            assert INGEST_URL not in repr(endpoint.content)
            assert INGEST_URL in endpoint.content.ingest_url  # readable when asked for

    def test_no_rendering_of_a_deliverys_headers_emits_a_sender_secret(self, clients):
        """The one model that used to print values: ``WebhookEventHeaders`` is a ``dict``
        subclass, so ``dict.__repr__`` rendered a sender's secrets in full (#246).

        On the *absence* of the secret rather than on a redaction spelling, so a change
        that reintroduces the values by some other route still fails here. One client:
        these headers hold no client, so iterating both would assert the same thing twice.
        """
        headers = delivery_with_sender_secrets(clients[0]).content.headers
        for rendering in (
            repr(headers),
            str(headers),
            f"{headers}",
            pprint.pformat(headers),
            repr(delivery_with_sender_secrets(clients[0])),
        ):
            for secret in SENDER_SECRETS.values():
                assert secret not in rendering

    def test_the_headers_repr_names_every_header_it_elides(self, clients):
        """The spelling, separately: elision is worthless if it also hides *which* headers
        arrived, because then debugging a delivery needs the values again."""
        headers = delivery_with_sender_secrets(clients[0]).content.headers
        assert repr(headers) == f"<WebhookEventHeaders {sorted(headers)}>"
        for name in SENDER_SECRETS:
            assert name in repr(headers)

    def test_a_webhook_header_read_is_still_wire_exact(self, clients):
        """The elision is a repr, not a read. Every documented lookup still returns what
        the wire carried, case-folded as #199 landed it — withholding the value from a
        caller who asked for it by name would be a different change, and not this one.
        """
        headers = delivery_with_sender_secrets(clients[0]).content.headers
        assert headers["Authorization"] == SENDER_SECRETS["Authorization"]
        assert headers["x-api-key"] == SENDER_SECRETS["X-Api-Key"]
        assert headers.get("AUTHORIZATION") == SENDER_SECRETS["Authorization"]
        assert dict(headers)["X-Api-Key"] == SENDER_SECRETS["X-Api-Key"]

    def test_the_paths_that_still_carry_the_values_are_the_documented_limit(self, clients):
        """The limit of this hardening, pinned so it stays documented rather than assumed.

        Every one of these is a caller *converting away from the class* or *asking for the
        pairs*, which is the deliberate path #246 deliberately left alone. Redacting here
        would change what a read returns, which is the thing the capital's decision
        explicitly did not do. Named exhaustively, because "and `json.dumps` too" would
        leave a reader guessing at the rest.
        """
        headers = delivery_with_sender_secrets(clients[0]).content.headers
        secret = SENDER_SECRETS["Authorization"]
        assert secret in json.dumps(headers)
        assert secret in repr(dict(headers))
        assert secret in repr({**headers})
        assert secret in repr(headers | {})
        assert secret in repr(list(headers.items()))
        assert secret in repr(list(headers.values()))
        # The pickle BYTES, not the round-trip: unpickling gives another of these,
        # whose repr elides again. It is the serialized form that carries the value.
        assert secret.encode() in pickle.dumps(headers)

    def test_a_logged_webhook_delivery_emits_no_sender_secret(self, clients, log_stream):
        """The accidental path the elision exists for: ``%r`` while debugging a delivery."""
        log, stream = log_stream
        event = delivery_with_sender_secrets(clients[0])
        log.debug("event=%r content=%r headers=%r", event, event.content, event.content.headers)

        captured = stream.getvalue()
        assert captured  # the handler really ran; without this the loop below is vacuous
        for secret in SENDER_SECRETS.values():
            assert secret not in captured

    def test_a_model_offers_no_generic_serialization(self, clients):
        """No ``to_dict``, not iterable: ``_client`` is reachable by no documented route."""
        for client in clients:
            model = ApiObject(timeline_payload(), client=client)
            with pytest.raises(AttributeError):
                model.to_dict()
            with pytest.raises(TypeError):
                iter(model)
            with pytest.raises(TypeError):
                dict(model)
            assert not hasattr(model, "keys")

    def test_equality_and_hashing_read_wire_data_only(self, clients):
        """So a failed comparison renders through ``__repr__``, which shows keys."""
        sync_client, async_client = clients
        one = ApiObject(timeline_payload(), client=sync_client)
        two = ApiObject(timeline_payload(), client=async_client)
        assert one == two  # different clients, same record
        assert hash(one) == hash(two)
        assert FAKE_TOKEN not in repr((one, two))


#: The built-ins whose ``__repr__`` prints the contents. Subclass one without overriding
#: it and the subclass prints values -- which is how #246 happened. ``str`` and ``bytes``
#: are in the list and are not hypothetical: ``ingest_url`` is a credential this SDK
#: already handles, and a ``str`` subclass wrapping one would print it in full.
VALUE_PRINTING_BUILTINS = (dict, list, tuple, set, frozenset, str, bytes, bytearray)


def test_no_exported_class_inherits_a_value_printing_repr():
    """By discovery, not by name — the check #246 existed because nobody had written.

    ``WebhookEventHeaders`` was the SDK's only built-in subclass and nothing asked whether
    it printed its contents, so for four releases it did. Naming it in a test would fix
    that one class; sweeping ``__all__`` fixes the next one too, the way
    ``TestEveryResourceIsCovered`` does for resources rather than trusting a hand list.

    What it does **not** catch, stated rather than implied: a class that writes its own
    value-printing ``__repr__`` (structure cannot tell that from a useful one), and a
    container class that is never exported. Both are for review to catch; this closes the
    one that is mechanical.
    """
    offenders = [
        f"{name} subclasses {builtin.__name__} and inherits its __repr__"
        for name in basecradle.__all__
        if isinstance(exported := getattr(basecradle, name), type)
        for builtin in VALUE_PRINTING_BUILTINS
        if issubclass(exported, builtin) and exported.__repr__ is builtin.__repr__
    ]
    assert not offenders, (
        f"{offenders} — a repr prints names, never values (#246). A built-in container's "
        f"own __repr__ prints the contents, so subclassing one without overriding it "
        f"exports a class that renders whatever it holds into any log that touches it."
    )


class TestLogging:
    def test_logging_a_client_resource_or_model_emits_no_credential(self, clients, log_stream):
        log, stream = log_stream
        for client in clients:
            model = ApiObject(timeline_payload(), client=client)
            log.debug("client=%r resource=%r model=%r", client, client.messages, model)
            log.debug("client=%s", client)
            log.debug("interpolated=%s", f"{client} {client.messages} {model}")
            log.debug("instance-dict=%r", vars(client))

        captured = stream.getvalue()
        assert captured  # the handler really ran; without this the assert below is vacuous
        assert FAKE_TOKEN not in captured


class TestExceptionsCarryNoCredential:
    def test_an_api_error_holds_only_the_problem_document(self, bc, api):
        """No ``httpx`` request or response on the error, so no ``Authorization`` header."""
        api.get("/timelines").respond(
            403,
            json=problem("not_a_viewer", 403),
            headers={"Content-Type": "application/problem+json"},
        )
        with pytest.raises(BaseCradleError) as caught:
            list(bc.timelines)

        error = caught.value
        assert not hasattr(error, "response")
        assert not hasattr(error, "request")
        for rendered in (repr(error), str(error), repr(vars(error))):
            assert FAKE_TOKEN not in rendered
        assert FAKE_TOKEN not in json.dumps(vars(error), default=str)

    def test_a_connection_error_names_the_base_url_and_nothing_else(self, bc, api):
        import httpx

        api.get("/timelines").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(APIConnectionError) as caught:
            list(bc.timelines)
        assert FAKE_TOKEN not in str(caught.value)
        assert FAKE_TOKEN not in repr(vars(caught.value))


class TestTheTokenStaysReadable:
    """Hardening must not cost the documented read — ``login()`` points callers at it."""

    def test_the_token_reads_back(self, clients):
        for client in clients:
            assert client.token == FAKE_TOKEN

    def test_the_token_is_still_assignable(self, clients):
        """Unchanged from when this was a plain attribute, transport semantics included."""
        replacement = "bc_uat_9xQ2mPvKdN4sT7yR1wZcB6hJ0aLgEuFo"
        for client in clients:
            before = client._client.headers["Authorization"]
            client.token = replacement
            assert client.token == replacement
            # The transport's header is built once, at construction -- assigning the
            # attribute never re-authenticated an existing client, and still does not.
            assert client._client.headers["Authorization"] == before
            client.token = FAKE_TOKEN

    def test_an_absent_token_reports_as_absent(self):
        """``hasattr``/``getattr``-with-default must keep working, and the error must be
        an ``AttributeError``.

        A slot with nothing in it raises ``AttributeError``, which is what the attribute
        protocol promises and the only exception ``getattr``'s default swallows. The first
        fix for this used a keyed store, whose miss was a ``KeyError`` — so ``hasattr``
        *raised* instead of returning ``False``, and the escaping exception was not a
        ``BaseCradleError`` despite the taxonomy promising to be the root of everything
        this SDK raises. Ironic for a change motivated by crash reporters, which guard
        only ``AttributeError`` when they probe an attribute.
        """
        for cls in (BaseCradle, AsyncBaseCradle):
            never_initialized = cls.__new__(cls)
            assert hasattr(never_initialized, "token") is False
            assert getattr(never_initialized, "token", None) is None
            with pytest.raises(AttributeError):
                never_initialized.token

    def test_a_subclass_inherits_the_slot_and_the_redaction(self):
        """Requested on #242: the property must survive a subclass, not just both clients.

        The slot is declared on ``_ClientCore`` while the concrete clients deliberately
        declare none — that is what gives them an ordinary ``__dict__`` for their
        resources. So a user subclass gets a ``__dict__`` too, and the question worth
        pinning is whether the credential stays in the inherited slot rather than falling
        into that dict. It does, including for a subclass that adds slots of its own.
        """

        class Plain(BaseCradle):
            pass

        class Slotted(AsyncBaseCradle):
            __slots__ = ("label",)

        class Overriding(BaseCradle):
            def __init__(self, token):
                super().__init__(token)
                self.extra = "set after super().__init__"

        for cls in (Plain, Slotted, Overriding):
            client = cls(token=FAKE_TOKEN)

            assert client.token == FAKE_TOKEN, cls.__name__
            assert "token" not in vars(client), cls.__name__
            assert "_token" not in vars(client), cls.__name__
            assert FAKE_TOKEN not in repr(vars(client)), cls.__name__

            assert repr(client) == (f"<{cls.__name__} base_url='{BASE_URL}' token=[REDACTED]>")

            replacement = "bc_uat_9xQ2mPvKdN4sT7yR1wZcB6hJ0aLgEuFo"
            client.token = replacement
            assert client.token == replacement, cls.__name__
            assert FAKE_TOKEN not in repr(vars(client)), cls.__name__

            with pytest.raises(BaseCradleError):
                pickle.dumps(client)

            if not client._is_async:
                client.close()

    def test_the_client_keeps_no_dict_entry_for_the_token(self, clients):
        """Nothing is left behind to clean up, and nothing outlives the client."""
        for client in clients:
            assert FAKE_TOKEN not in repr(vars(client))
            assert FAKE_TOKEN not in repr(sorted(vars(client)))
