"""The model layer: how wire JSON becomes typed Python objects.

Every model is a read-only, wire-exact view of an API JSON object — attribute names
mirror the API's JSON exactly. Class-level annotations document the contract (and drive
nested wrapping); attribute access reads the wire data itself, so:

- A field the API added after this SDK release is readable immediately (the API is
  additive-only — the SDK never hides what the platform says).
- A field the API did not return (access-gated, e.g. a User's trusted-peer cluster)
  raises ``AttributeError`` with an explanation — never a silent ``None`` that could
  mean "hidden from you" or "actually null".
"""

from __future__ import annotations

import functools
import typing
from typing import Any

__all__ = ["ApiObject"]


class ApiObject:
    """A read-only, wire-exact view of one API JSON object."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

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

        value = data[name]
        nested_class = _nested_classes(type(self)).get(name)
        if nested_class is not None and isinstance(value, dict):
            return nested_class(value)
        return value

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
    """Which annotated fields of ``cls`` are themselves models (and should auto-wrap).

    Resolved from the class's type annotations, once per class.
    """
    return {
        name: annotation
        for name, annotation in typing.get_type_hints(cls).items()
        if isinstance(annotation, type) and issubclass(annotation, ApiObject)
    }
