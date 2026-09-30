"""What every resource shares: a safe repr, and a refusal to be serialized.

A resource — ``bc.timelines``, ``bc.messages``, ``timeline.tasks`` — is a live handle, not
a record. Two facts about it do not survive a generic serializer:

- **It holds the client**, so the account's bearer token is a short walk away. The client
  keeps that token out of its own ``__dict__`` (see ``_client._ClientCore``), but a pickled
  resource is a credential at rest either way.
- **Its records are lazy.** A list resource carries a path and its filters and nothing
  else; the records arrive a page at a time, when you iterate. There is no content in it to
  serialize — only a promise to go and fetch some.

So resources refuse the serialization protocol outright and say why, instead of leaving it
to the accident that ``httpx`` happens to hold an unpicklable lock (#242).
"""

from __future__ import annotations

from typing import Any, NoReturn

from basecradle._exceptions import BaseCradleError

__all__ = ["Resource", "refuse_serialization"]


def refuse_serialization(message: str) -> NoReturn:
    """Raise the refusal. Shared by ``Resource`` and the client, which word it differently.

    Both hang it on ``__reduce__``, which is the one hook that covers the family:
    ``pickle.dumps`` at every protocol, ``copy.copy``, ``copy.deepcopy``, and a direct
    ``__reduce_ex__``. It is not absolute, and deliberately not claimed to be — a caller
    who registers its own reducer in ``copyreg.dispatch_table`` or overrides
    ``Pickler.reducer_override`` is consulted *before* ``__reduce__`` and bypasses this
    entirely. That is a caller overriding on purpose, not a hole to plug.
    """
    raise BaseCradleError(message)


class Resource:
    """Base of every resource: a repr that names what this is, and no serialization."""

    def __repr__(self) -> str:
        fields = "".join(f" {name}={value!r}" for name, value in self._repr_fields().items())
        return f"<{type(self).__name__}{fields}>"

    def _repr_fields(self) -> dict[str, Any]:
        """What this resource's repr shows after its class name — never the client.

        Only what the object actually carries: a list resource has its path and filters, a
        nested creator has its timeline, and the three that hold nothing but the client
        show nothing. Default empty so a new resource is safe before it is interesting.

        Whatever an override returns, it must not raise: ``logging`` discards the entire
        log record when a ``%r`` argument's repr blows up, so a repr added to keep
        credentials *out* of the logs would be taking the logs down instead.
        """
        return {}

    def __reduce__(self) -> NoReturn:
        """Refuse the serializers that go through the pickle protocol, and say why."""
        refuse_serialization(
            f"A {type(self).__name__} cannot be serialized. It is a live handle on a "
            f"BaseCradle client — a short walk from your bearer token — and its records "
            f"are fetched lazily, a page at a time, so there is nothing in it to "
            f"serialize yet. Iterate it explicitly and keep the records instead: "
            f"`list(...)`, or `for record in ...`."
        )
