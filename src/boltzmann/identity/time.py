"""Timestamps that survive being hashed.

A timestamp inside a block payload is part of that block's identity, so it needs
exactly one textual form. Python's ``datetime.isoformat`` does not give one:
``+00:00`` and ``Z`` denote the same instant, and microseconds appear only when
non-zero. Two clients holding the same instant would compute different
``block_id`` values.

The protocol therefore stores timestamps as **RFC 3339 strings in UTC with
second precision**, written ``YYYY-MM-DDTHH:MM:SSZ``. Anything else is rejected
at validation; use :func:`utc_timestamp` to produce the canonical form.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

from pydantic import StringConstraints

RFC3339_UTC_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
"""RFC 3339 in UTC with second precision: the only accepted timestamp form."""

Timestamp = Annotated[str, StringConstraints(pattern=RFC3339_UTC_PATTERN)]
"""A canonical UTC timestamp string, safe to place inside a hashed payload."""


def utc_timestamp(moment: datetime | None = None) -> str:
    """
    Format an instant as a canonical Boltzmann timestamp.

    Args:
        moment (datetime | None): The instant to format. Naive values are read as
            UTC; aware values are converted. Defaults to now.

    Returns:
        str: The instant as ``YYYY-MM-DDTHH:MM:SSZ``, truncated to the second.
    """
    if moment is None:
        moment = datetime.now(UTC)
    elif moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: str) -> datetime:
    """
    Parse a canonical Boltzmann timestamp back into an aware datetime.

    Args:
        value (str): A timestamp in ``YYYY-MM-DDTHH:MM:SSZ`` form.

    Returns:
        datetime: The instant, in UTC.

    Raises:
        ValueError: If ``value`` is not in the canonical form.
    """
    if not re.match(RFC3339_UTC_PATTERN, value):
        raise ValueError(f"not a canonical UTC timestamp: {value!r}")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
