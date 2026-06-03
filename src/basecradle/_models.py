"""The model layer: how wire JSON becomes typed Python objects.

Every model is a read-only, wire-exact view of an API JSON object — attribute names
mirror the API's JSON exactly. Class-level annotations document the contract (and drive
nested wrapping); attribute access reads the wire data itself, so:

- A field the API added after this SDK release is readable immediately (the API is
  additive-only — the SDK never hides what the platform says).
- A field the API did not return (access-gated, e.g. a User's trusted-peer cluster)
  raises ``AttributeError`` with an explanation — never a silent ``None`` that could
  mean "hidden from you" or "actually null".

Models built by the client carry a reference to it, so resource verbs
(``timeline.lock()``) can act on the platform.
"""

from __future__ import annotations

import functools
import typing
from collections.abc import Callable
from typing import Any

__all__ = ["ApiObject"]


class ApiObject:
    """A read-only, wire-exact view of one API JSON object."""

    def __init__(self, data: dict[str, Any], *, client: Any = None) -> None:
        self._data = data
        self._client = client

    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute lookup fails — i.e. for wire fields.
        if name.startswith("_"):
            raise AttributeError(name)

        data = object.__getattribute__(self, "_data")
        if name not in data:
            raise AttributeError(
                f"The API did not return {name!r} for this {type(self).__name__}. "
                f"It may be access-gated (see 'User access tiers' in the API docs) or not "
                f"part of this response form. Fields present: {sorted(data)}"
            )

        return self._wrap(name, data[name])

    def _wrap(self, name: str, value: Any) -> Any:
        """Wrap dicts (and lists of dicts) in their annotated model class, if any."""
        nested_class = _nested_classes(type(self)).get(name)
        if nested_class is None:
            return value
        if isinstance(value, dict):
            return nested_class(value, client=self._client)
        if isinstance(value, list):
            return [
                nested_class(item, client=self._client) if isinstance(item, dict) else item
                for item in value
            ]
        return value

    def _require_client(self) -> Any:
        """The client this object came from — required by verbs that call the API."""
        if self._client is None:
            raise RuntimeError(
                f"This {type(self).__name__} is not attached to a BaseCradle client, so it "
                f"cannot call the API. Objects obtained from a client (bc.timelines, bc.me, ...) "
                f"are attached automatically."
            )
        return self._client

    def _verb(self, method: str, path: str, apply: Callable[[Any], Any], **request_kwargs: Any):
        """Run a verb against the API — the sync/async dispatch, written once.

        Attached to a sync client: executes now and returns ``apply``'s result.
        Attached to an async client: returns a coroutine — the caller awaits it.
        """
        client = self._require_client()
        if client._is_async:
            return self._averb(client, method, path, apply, **request_kwargs)
        return apply(client.request(method, path, **request_kwargs))

    async def _averb(
        self, client: Any, method: str, path: str, apply: Callable[[Any], Any], **kwargs: Any
    ):
        return apply(await client.request(method, path, **kwargs))

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {sorted(self._data)}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ApiObject):
            return NotImplemented
        return type(self) is type(other) and self._data == other._data

    def __hash__(self) -> int:
        return hash((type(self), tuple(sorted(self._data))))


@functools.lru_cache(maxsize=None)
def _nested_classes(cls: type) -> dict[str, type[ApiObject]]:
    """Which annotated fields of ``cls`` are models (or lists of models) to auto-wrap.

    Resolved from the class's type annotations, once per class. Recognizes both
    ``field: Model`` and ``field: list[Model]``.
    """
    nested: dict[str, type[ApiObject]] = {}
    for name, annotation in typing.get_type_hints(cls).items():
        if isinstance(annotation, type) and issubclass(annotation, ApiObject):
            nested[name] = annotation
        elif typing.get_origin(annotation) is list:
            (element_type,) = typing.get_args(annotation)
            if isinstance(element_type, type) and issubclass(element_type, ApiObject):
                nested[name] = element_type
    return nested
