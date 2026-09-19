"""Dependency graph: turn declared and implicit references into one deterministic order.

Combines a manifest's ``depends_on`` (explicit) and ``${...}`` references
(implicit) into a single apply order.

Pure functions only -- no I/O, no engine state. The engine calls
``topological_order`` once per ``apply``/``plan`` and walks the result in
order; it never reasons about the graph itself.
"""

from collections.abc import Sequence
from typing import overload

from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.stack import ResourceSpec, ResourceState, StackManifest
from aws_admin_cli.domain.services.interpolation import REFERENCE_TOKEN_RE

__all__ = ["build_graph", "destroy_order", "extract_references", "topological_order"]


def extract_references(value: object) -> set[str]:
    """Recursively find every ``${<id>.<key>}`` reference within ``value``.

    Walks dicts, lists, and strings (any other type -- int, bool, None --
    contributes nothing). This is what makes a dependency IMPLICIT: a
    resource whose properties use ``${app-role.arn}`` depends on
    ``app-role`` even if it never lists it in ``depends_on``. Escaped
    references (``$${...}``) are never real references and are ignored, same
    grammar as ``domain/services/interpolation.py``.

    Args:
        value: Typically a resource's ``properties`` dict, but works on any
            nested structure.

    Returns:
        The set of logical ids referenced (possibly empty).
    """
    ids: set[str] = set()
    _collect_references(value, ids)
    return ids


def _collect_references(value: object, ids: set[str]) -> None:
    if isinstance(value, str):
        for match in REFERENCE_TOKEN_RE.finditer(value):
            resource_id = match.group("id")
            if resource_id is not None:
                ids.add(resource_id)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_references(item, ids)
    elif isinstance(value, list):
        for item in value:
            _collect_references(item, ids)


def build_graph(manifest: StackManifest) -> dict[str, set[str]]:
    """Build the full dependency graph: explicit ``depends_on`` union implicit references.

    Returns:
        A mapping of logical id -> the set of ids it depends on (must be
        created before it). May contain ids not present in the manifest --
        ``topological_order`` is what validates and reports those.
    """
    return {
        resource.id: set(resource.depends_on) | extract_references(resource.properties)
        for resource in manifest.resources
    }


def _validate_known_ids(graph: dict[str, set[str]], valid_ids: set[str]) -> None:
    for resource_id, deps in graph.items():
        unknown = deps - valid_ids
        if unknown:
            raise ValidationError(
                f"El recurso '{resource_id}' depende de id(s) que no existen en este "
                f"manifiesto: {', '.join(sorted(unknown))}.",
                hint="Ids disponibles: " + ", ".join(sorted(valid_ids)),
            )


def _find_cycle(graph: dict[str, set[str]]) -> list[str]:
    """Find one concrete cycle within ``graph`` via DFS, e.g. ``["a", "b", "c", "a"]``.

    Only called once Kahn's algorithm has already determined a cycle exists
    somewhere among ``graph``'s nodes -- this just needs to name one.
    Deterministic (sorted traversal) so the reported cycle doesn't vary
    between runs on the same stuck subgraph.
    """
    visiting: list[str] = []
    visited: set[str] = set()

    def dfs(node: str) -> list[str] | None:
        if node in visiting:
            start = visiting.index(node)
            return [*visiting[start:], node]
        if node in visited:
            return None
        visiting.append(node)
        for dep in sorted(graph.get(node, ())):
            found = dfs(dep)
            if found is not None:
                return found
        visiting.pop()
        visited.add(node)
        return None

    for node in sorted(graph):
        found = dfs(node)
        if found is not None:
            return found
    return sorted(graph)  # pragma: no cover -- unreachable: Kahn's algorithm already proved a cycle


def topological_order(manifest: StackManifest) -> list[ResourceSpec]:
    """Order ``manifest``'s resources so every dependency comes before its dependents.

    DETERMINISTIC: ties (multiple resources simultaneously ready) always
    resolve alphabetically by id, so the same manifest produces the exact
    same plan every time -- a `plan` a human reviews has to be trustworthy
    as a *document*, not just a valid ordering.

    Raises:
        ValidationError: A ``depends_on`` entry or a ``${id.key}`` reference
            names an id that doesn't exist in this manifest (lists the valid
            ids), or the graph has a circular dependency (names the concrete
            cycle, e.g. ``"a → b → c → a"``, never a generic "there's a cycle").
    """
    graph = build_graph(manifest)
    valid_ids = {resource.id for resource in manifest.resources}
    _validate_known_ids(graph, valid_ids)

    remaining = {resource_id: set(deps) for resource_id, deps in graph.items()}
    order: list[str] = []

    while remaining:
        ready = sorted(resource_id for resource_id, deps in remaining.items() if not deps)
        if not ready:
            cycle = _find_cycle(remaining)
            raise ValidationError(
                f"El manifiesto tiene una dependencia circular: {' → '.join(cycle)}.",
                hint="Revisa `depends_on` y las referencias '${...}' en las properties "
                "de esos recursos.",
            )
        current = ready[0]
        order.append(current)
        del remaining[current]
        for deps in remaining.values():
            deps.discard(current)

    by_id = {resource.id: resource for resource in manifest.resources}
    return [by_id[resource_id] for resource_id in order]


@overload
def destroy_order(source: StackManifest) -> list[ResourceSpec]: ...
@overload
def destroy_order(source: Sequence[ResourceState]) -> list[ResourceState]: ...
def destroy_order(
    source: StackManifest | Sequence[ResourceState],
) -> list[ResourceSpec] | list[ResourceState]:
    """Return the exact reverse of the apply order.

    Args:
        source: Either a ``StackManifest`` (a fresh topological order is
            computed and reversed) or a ``StackState``'s ``resources`` list
            (already in apply order, since ``StackEngine.apply`` appends to
            it in topological order as it goes -- simply reversed here).
    """
    if isinstance(source, StackManifest):
        return list(reversed(topological_order(source)))
    return list(reversed(list(source)))
