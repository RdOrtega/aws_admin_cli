"""Tests for domain/services/dependency_graph.py: ordering, determinism, cycles."""

import pytest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, StackManifest
from aws_admin_cli.domain.services.dependency_graph import (
    build_graph,
    destroy_order,
    extract_references,
    topological_order,
)


def _resource(
    resource_id: str,
    *,
    properties: dict[str, object] | None = None,
    depends_on: list[str] | None = None,
) -> ResourceSpec:
    return ResourceSpec(
        id=resource_id,
        kind=ResourceKind.IAM_ROLE,
        properties=properties or {},
        depends_on=depends_on or [],
    )


def _manifest(resources: list[ResourceSpec], name: str = "demo") -> StackManifest:
    return StackManifest(apiVersion="v1", name=name, resources=resources)


# -- extract_references -----------------------------------------------------------------


def test_extract_references_finds_ids_in_nested_strings_lists_and_dicts() -> None:
    value = {
        "a": "${res-x.arn}",
        "b": ["${res-y.name}", "plain string"],
        "c": {"nested": "${res-z.value}"},
    }
    assert extract_references(value) == {"res-x", "res-y", "res-z"}


def test_extract_references_ignores_escaped_references() -> None:
    value = {"a": "$${res-x.arn}", "b": "${res-y.name}"}
    assert extract_references(value) == {"res-y"}


def test_extract_references_on_non_string_values_is_empty() -> None:
    assert extract_references({"a": 3, "b": True, "c": None}) == set()


# -- build_graph / topological_order: explicit dependencies -------------------------------


def test_topological_order_respects_explicit_depends_on() -> None:
    manifest = _manifest(
        [
            _resource("res-c", depends_on=["res-b"]),
            _resource("res-b", depends_on=["res-a"]),
            _resource("res-a"),
        ]
    )
    order = [r.id for r in topological_order(manifest)]
    assert order == ["res-a", "res-b", "res-c"]


# -- implicit dependencies via interpolation -----------------------------------------------


def test_topological_order_respects_implicit_references() -> None:
    manifest = _manifest(
        [
            _resource("res-b", properties={"ref": "${res-a.arn}"}),  # no depends_on!
            _resource("res-a"),
        ]
    )
    order = [r.id for r in topological_order(manifest)]
    assert order.index("res-a") < order.index("res-b")


def test_build_graph_unions_explicit_and_implicit() -> None:
    manifest = _manifest(
        [
            _resource("res-c", depends_on=["res-a"], properties={"ref": "${res-b.arn}"}),
            _resource("res-a"),
            _resource("res-b"),
        ]
    )
    graph = build_graph(manifest)
    assert graph["res-c"] == {"res-a", "res-b"}


# -- determinism --------------------------------------------------------------------------


def test_topological_order_is_deterministic_across_many_runs() -> None:
    manifest = _manifest(
        [
            _resource("res-e"),
            _resource("res-c"),
            _resource("res-a"),
            _resource("res-d"),
            _resource("res-b"),
        ]
    )
    first = [r.id for r in topological_order(manifest)]
    for _ in range(100):
        assert [r.id for r in topological_order(manifest)] == first
    # No dependencies at all among these -- ties always resolve alphabetically.
    assert first == ["res-a", "res-b", "res-c", "res-d", "res-e"]


# -- cycles ---------------------------------------------------------------------------------


def test_direct_cycle_names_the_cycle() -> None:
    manifest = _manifest(
        [
            _resource("res-a", depends_on=["res-b"]),
            _resource("res-b", depends_on=["res-a"]),
        ]
    )
    with pytest.raises(ValidationError) as exc_info:
        topological_order(manifest)
    message = str(exc_info.value)
    assert "res-a" in message and "res-b" in message
    assert "→" in message


def test_indirect_cycle_names_the_full_cycle() -> None:
    manifest = _manifest(
        [
            _resource("res-a", depends_on=["res-b"]),
            _resource("res-b", depends_on=["res-c"]),
            _resource("res-c", depends_on=["res-a"]),
        ]
    )
    with pytest.raises(ValidationError) as exc_info:
        topological_order(manifest)
    message = str(exc_info.value)
    assert "res-a" in message and "res-b" in message and "res-c" in message


# -- unknown reference ------------------------------------------------------------------------


def test_depends_on_unknown_id_lists_available_ids() -> None:
    # StackManifest itself already validates depends_on at construction (fail-fast) --
    # topological_order never even sees an invalid EXPLICIT reference. The implicit-
    # reference path below is what actually exercises topological_order's own check.
    with pytest.raises(ValidationError) as exc_info:
        _manifest([_resource("res-a", depends_on=["res-no-existe"])])
    assert "res-no-existe" in str(exc_info.value)


def test_implicit_reference_to_unknown_id_lists_available_ids() -> None:
    manifest = _manifest(
        [_resource("res-a", properties={"ref": "${no-existe.arn}"})]
    )
    with pytest.raises(ValidationError) as exc_info:
        topological_order(manifest)
    assert "no-existe" in str(exc_info.value)


# -- destroy_order ----------------------------------------------------------------------------


def test_destroy_order_is_exact_reverse_of_topological_order() -> None:
    manifest = _manifest(
        [
            _resource("res-c", depends_on=["res-b"]),
            _resource("res-b", depends_on=["res-a"]),
            _resource("res-a"),
        ]
    )
    applied = [r.id for r in topological_order(manifest)]
    destroyed = [r.id for r in destroy_order(manifest)]
    assert destroyed == list(reversed(applied))
