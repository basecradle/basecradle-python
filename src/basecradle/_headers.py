"""Inbound request headers, as the platform stores them — one class for every record that has them.

Two records carry the headers of an HTTP request the platform received: a webhook event
(an external sender's delivery) and a contact message (a visitor's contact-form submission).
Both are stored the same way — names canonicalized to Title-Case per segment — and both
can carry somebody else's secret, so both read through ``RequestHeaders``: lookup folds
case, and ``repr()`` prints names, never values (#246).
"""

from __future__ import annotations

from typing import Any, TypeVar, overload

_Default = TypeVar("_Default")
#: The value type. A webhook delivery's headers are all strings; a contact message's may be
#: ``null`` on the wire, so it is ``RequestHeaders[str | None]`` and says so.
_V = TypeVar("_V")
_H = TypeVar("_H", bound="RequestHeaders[Any]")

__all__ = ["RequestHeaders"]


class RequestHeaders(dict[str, _V]):
    """One request's headers: the wire's own spelling, looked up case-insensitively.

    A plain ``dict`` of exactly what the wire carried — one pair per header — so iterating,
    ``keys()`` and ``==`` all read the platform's own spelling and nothing is renamed. Only
    **lookup** folds case, because header names are case-insensitive by RFC and the
    platform does not preserve the sender's casing: it stores names canonicalized to
    Title-Case per segment (``X-Github-Delivery``, not ``X-GitHub-Delivery``). So a vendor's
    published spelling finds the header it names, and so does any other casing of it::

        headers["X-GitHub-Delivery"]  # GitHub's own published spelling
        headers["x-github-delivery"]  # the lowercase form
        headers["X-Github-Delivery"]  # what the wire actually carried

    ``in`` and ``get()`` fold case the same way, and ``copy()`` gives another object of the
    same class. A header that was genuinely not sent is **absent**, never ``None``:
    subscripting raises ``KeyError`` naming the headers that did arrive, and ``get()``
    returns its default — plain ``dict`` behavior. Converting away from this type gives up
    the case folding (``dict(headers)``, ``{**headers}``, ``headers | other``), and so does
    writing to it: this is a read of one request that already happened, so the ``dict``
    mutators are left exactly as ``dict`` defines them, case-sensitive. Converting away
    also gives up the value elision below, which is the more consequential of the two.

    **``repr()`` prints the header names and elides the values**, the way every
    ``ApiObject`` in this SDK already does — a repr prints names, never values (#246).
    Reads are untouched and stay wire-exact.

    Generic over the value type, because the two records differ there and the annotation
    should not claim otherwise: ``WebhookEventHeaders`` (a webhook delivery's, and the name
    0.11.0 exported) is ``RequestHeaders[str]``, and a contact message's ``headers`` is
    ``RequestHeaders[str | None]`` — the platform can record a header with a ``null`` value
    there, and ``headers["Host"]`` then reads ``None``. On those, ``get()`` returning
    ``None`` does not by itself mean "not sent"; ``in`` does.
    """

    def __repr__(self) -> str:
        """The header names this request carried, wire spelling, without their values.

        Inherited, ``dict.__repr__`` would print every value in full, and these values are
        not ours: a sender authenticating its POST to an ingest URL puts *its* secret in
        them (``Authorization``, ``X-Api-Key``, a signing header). One ``log.debug("%r",
        event.content.headers)`` while debugging a delivery and that secret is wherever
        the logs are -- and the party harmed is the sender, who never agreed to our
        logging (#246, surfaced by the #242 sweep).

        Not a denylist of sensitive names: the next vendor's header would not be on it.
        Names only, all of them, the way ``ApiObject.__repr__`` renders every other model
        -- so this class stops being the one that prints values. The deliberate read is
        untouched: ``headers["X-GitHub-Delivery"]`` returns exactly what the wire carried,
        and so does ``json.dumps(headers)``, which is still a real ``dict``.

        ``key=str`` so the sort is total across mixed key types. The ``dict`` mutators are
        deliberately left as ``dict`` defines them (see the class docstring), so a
        non-string key is reachable, and a bare ``sorted`` would blow up on ``int < str``
        -- in a repr, which ``logging`` discards the whole record over. A repr added to
        keep a secret out of the logs would be taking the logs down instead
        (``_resources.py`` -> ``_repr_fields`` states the same rule for resources).

        That covers the reachable case, not every conceivable one: a key whose own
        ``__str__`` or ``__repr__`` raises still propagates, because there is no rendering
        of it to fall back to. Header names are strings; this is about not breaking on the
        near-miss, not about surviving an arbitrary object.
        """
        return f"<{type(self).__name__} {sorted(self, key=str)}>"

    def __getitem__(self, name: str) -> _V:
        wire_name = self._wire_name(name)
        if wire_name is None:
            raise KeyError(
                f"No {name!r} header on this request. Header names are matched "
                f"case-insensitively, so no casing of it was sent either. "
                # The string keys only -- these are the header NAMES that arrived, and
                # a non-string key (reachable through the mutators) is not one: listing
                # it would have this message contradict its own first sentence, since
                # `_wire_name` reports exactly those keys as absent. Filtering also makes
                # the sort total, which a bare sorted() here would not be.
                f"Headers present: {sorted(n for n in self if isinstance(n, str))}"
            )
        return super().__getitem__(wire_name)

    # mypy baseline: these overloads deliberately restate `dict.get`'s contract more
    # narrowly -- keys here are header names, so `str`. Widening them to match
    # `dict.get` exactly is the opposite of what #199 landed this class for.
    @overload  # type: ignore[override]
    def get(self, name: str) -> _V | None: ...

    @overload
    def get(self, name: str, default: _Default) -> _V | _Default: ...

    def get(self, name: str, default: Any = None) -> Any:
        """The header's value, matched case-insensitively, or ``default`` if not sent.

        Overloaded rather than left at ``Any``: ``dict.get`` declares ``_V | None`` with no
        default and ``_V | _Default`` with one, and a ``py.typed`` SDK must not widen that
        — ``int(headers.get("Content-Length"))`` has to stay the type error it is.
        """
        wire_name = self._wire_name(name)
        return default if wire_name is None else super().__getitem__(wire_name)

    def __contains__(self, name: object) -> bool:
        return self._wire_name(name) is not None

    def copy(self: _H) -> _H:
        """Another headers object of this same class — ``dict.copy()`` would silently
        downgrade to a plain dict."""
        return type(self)(self)

    def _wire_name(self, name: object) -> str | None:
        """The wire's own spelling of ``name``, or ``None`` if no casing of it was sent.

        Anything but a string is simply not a header name, so it reads as absent rather than
        blowing up in ``str.lower`` — ``headers[object()]`` owes a ``KeyError``.
        """
        if not isinstance(name, str):
            return None
        if super().__contains__(name):
            return name
        folded = name.lower()
        # The stored keys are checked too, not just the one being looked up. The `dict`
        # mutators are deliberately left open (see the class docstring), so a non-string
        # key is reachable -- and a bare `.lower()` over them would turn every subsequent
        # lookup into an AttributeError, including the ones this method promises to
        # answer with a plain "absent".
        return next(
            (
                wire_name
                for wire_name in self
                if isinstance(wire_name, str) and wire_name.lower() == folded
            ),
            None,
        )
