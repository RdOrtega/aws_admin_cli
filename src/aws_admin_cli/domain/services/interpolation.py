"""Interpolation: substitute ``${<logical_id>.<output_key>}`` references in stack properties.

Pure functions only -- no I/O, no boto3, no engine state. ``resolve_properties``
takes a resource's raw ``properties`` (straight from the manifest) plus every
already-applied resource's outputs, and returns the same shape with every
reference substituted. Implemented with ``re`` alone, deliberately: this
grammar is one construct (a reference, or an escaped literal), not a template
language, and pulling in a templating engine for it would be solving a
problem this project doesn't have.

Grammar:

- ``${id.key}`` -- a reference. ``id`` must be a valid resource id (see
  ``domain/models/stack.py``'s ``ResourceSpec.id`` pattern); ``key`` is
  whatever output name that resource's step exposes.
- ``$${...}`` -- an escaped literal: collapses to a literal ``${...}``,
  never treated as a reference. The escape hatch for a property that
  legitimately needs a literal ``${`` in its value (e.g. a shell/Make
  variable inside a ``user_data`` script).
"""

import re
from collections.abc import Mapping
from typing import Any, Self

from aws_admin_cli.core.exceptions import ValidationError

__all__ = ["REFERENCE_TOKEN_RE", "resolve_properties"]

# Two alternatives, tried in order: an escaped literal ($${...}, captured as
# `escaped`) or a real reference (${id.key}, captured as `id`/`key`). Exposed
# (not underscore-prefixed) so `domain/services/dependency_graph.py` can walk
# the exact same grammar for implicit-dependency extraction -- one source of
# truth for what "${...}" means, never two regexes that could drift apart.
REFERENCE_TOKEN_RE = re.compile(
    r"\$\$(?P<escaped>\{[^}]*\})"
    r"|\$\{(?P<id>[a-z][a-z0-9-]{0,62})\.(?P<key>[A-Za-z0-9_]+)\}"
)


class _UnknownResourceIdError(Exception):
    """Internal signal: a reference named a resource id not present in ``outputs``."""

    def __init__(self: Self, resource_id: str) -> None:
        self.resource_id = resource_id


class _UnknownOutputKeyError(Exception):
    """Internal signal: a reference named a real resource but an output key it doesn't expose."""

    def __init__(self: Self, resource_id: str, key: str) -> None:
        self.resource_id = resource_id
        self.key = key


def _lookup(resource_id: str, key: str, outputs: Mapping[str, Mapping[str, str]]) -> str:
    resource_outputs = outputs.get(resource_id)
    if resource_outputs is None:
        raise _UnknownResourceIdError(resource_id)
    if key not in resource_outputs:
        raise _UnknownOutputKeyError(resource_id, key)
    return resource_outputs[key]


def _raise_unknown_id(
    resource_id: str, outputs: Mapping[str, Mapping[str, str]]
) -> ValidationError:
    available = ", ".join(sorted(outputs)) if outputs else "(ninguno todavía)"
    return ValidationError(
        f"'{resource_id}' no es un id de recurso conocido en este stack (referenciado "
        "como '${" + resource_id + ".<key>}').",
        hint=f"Ids con outputs disponibles en este punto del apply: {available}.",
    )


def _raise_unknown_key(
    resource_id: str, key: str, outputs: Mapping[str, Mapping[str, str]]
) -> ValidationError:
    exposed = outputs.get(resource_id, {})
    available = ", ".join(sorted(exposed)) if exposed else "(ninguno)"
    return ValidationError(
        f"El recurso '{resource_id}' no expone el output '{key}' "
        "(referenciado como '${" + f"{resource_id}.{key}" + "}').",
        hint=f"Outputs que sí expone '{resource_id}': {available}.",
    )


def _substitute_string(text: str, outputs: Mapping[str, Mapping[str, str]]) -> Any:
    # The whole string is exactly one reference: return the output's raw value,
    # not a string built by concatenation -- "preserves the output's type" in
    # the sense that matters here (no stray characters glued around it).
    full = REFERENCE_TOKEN_RE.fullmatch(text)
    if full is not None and full.group("id") is not None:
        try:
            return _lookup(full.group("id"), full.group("key"), outputs)
        except _UnknownResourceIdError as exc:
            raise _raise_unknown_id(exc.resource_id, outputs) from exc
        except _UnknownOutputKeyError as exc:
            raise _raise_unknown_key(exc.resource_id, exc.key, outputs) from exc

    error: ValidationError | None = None

    def _replace(match: re.Match[str]) -> str:
        nonlocal error
        if match.group("escaped") is not None:
            return "$" + match.group("escaped")
        try:
            return _lookup(match.group("id"), match.group("key"), outputs)
        except _UnknownResourceIdError as exc:
            error = _raise_unknown_id(exc.resource_id, outputs)
            return ""
        except _UnknownOutputKeyError as exc:
            error = _raise_unknown_key(exc.resource_id, exc.key, outputs)
            return ""

    result = REFERENCE_TOKEN_RE.sub(_replace, text)
    if error is not None:
        raise error
    return result


def _resolve_value(value: Any, outputs: Mapping[str, Mapping[str, str]]) -> Any:
    if isinstance(value, str):
        return _substitute_string(value, outputs)
    if isinstance(value, dict):
        return {key: _resolve_value(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, outputs) for item in value]
    # int, bool, float, None, ...: not a string, nothing to interpolate.
    return value


def resolve_properties(
    properties: Mapping[str, Any], outputs: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    """Recursively substitute every ``${id.key}`` reference in ``properties``.

    Args:
        properties: A resource's raw properties, straight from the manifest
            (already-parsed YAML: dicts, lists, strings, ints, bools, ...).
        outputs: Every already-applied resource's outputs, keyed by logical
            id -- typically ``{r.logical_id: r.outputs for r in
            already_applied_resources}``.

    Returns:
        The same shape as ``properties``, with every string value's
        references substituted (escaped ``$${...}`` collapsed to a literal
        ``${...}``) and every non-string value passed through unchanged.

    Raises:
        ValidationError: A reference names a resource id not present in
            ``outputs``, or an output key that resource doesn't expose --
            both list what IS available, since this is the error message a
            manifest author will hit most often while iterating.
    """
    return {key: _resolve_value(value, outputs) for key, value in properties.items()}
