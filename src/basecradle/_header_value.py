"""The one rule for a value the SDK sends in a header exactly as given (#351, #359).

An idempotency key and the bearer token are both sent as the caller gave them, never
stripped or repaired, so both must be a header value no hop can change. The rule lives
here, below the client and the resources, so both can ask it.
"""

from __future__ import annotations


def _header_value_defect(value: str) -> tuple[int | None, str] | None:
    """What stops HTTP carrying ``value`` exactly as given, or ``None`` when nothing does.

    The value must be printable ASCII (``!`` to ``~``), with spaces or tabs allowed only
    between those characters. HTTP cannot carry a non-ASCII character, NUL, CR or LF.
    Whitespace at either end is not part of the value: a hop strips it, so the platform
    would read a different value. (h11 refuses it at the end of a header; at the start of
    a token it lands mid-header, after ``Bearer``, and goes out, which is why the rule is
    about the value the caller gave and not the header line.)
    Other control characters are refused as well, though h11 would send them, because the
    rule is the same in every SDK and not whatever one HTTP library tolerates.

    The answer is ``(index, reason)``: the index of the character at fault, or ``None`` for
    whitespace at either end, where no one character is. The character itself is never
    part of the answer, so a caller describing the token can say where the fault is
    without showing any of it.
    """
    for index, char in enumerate(value):
        if not char.isascii():
            return index, "is not ASCII"
        if char not in " \t" and not "!" <= char <= "~":
            return index, "is a control character"
    if value.startswith((" ", "\t")) or value.endswith((" ", "\t")):
        return None, "it begins or ends with whitespace, which a header value cannot"
    return None
