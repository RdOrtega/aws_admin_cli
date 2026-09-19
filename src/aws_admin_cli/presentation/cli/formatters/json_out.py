"""JSON rendering: canonical JSON serialization for machine-readable output."""

import json
from typing import Any


def to_json(data: Any) -> str:
    """Serialize ``data`` to an indented, UTF-8-safe JSON string.

    Args:
        data: Any JSON-serializable value. Non-serializable values (``Path``,
            ``datetime``, enum members, ...) fall back to ``str()``.

    Returns:
        The JSON string, ready to print to STDOUT.
    """
    return json.dumps(data, indent=2, default=str, ensure_ascii=False, sort_keys=False)
