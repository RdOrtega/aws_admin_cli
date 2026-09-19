"""Enforces the stack engine's own Separation of Duties: no manifest can declare network.

Same spirit as ``test_vpc_readonly.py`` -- see that file's module docstring
for the full reasoning. A stack REFERENCES existing VPCs/subnets/security
groups by name (via ``NetworkResolver``, read-only); it never creates,
modifies, or deletes one. If you're reading this because a check below just
failed: that is the point. The fix is almost never to adjust this test --
it's to remove whatever ``vpc:``/``subnet:``/``security-group:`` member or
mutating call you just added.
"""

import inspect
from pathlib import Path

import pytest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.stack import ResourceKind
from aws_admin_cli.infrastructure.manifests.yaml_loader import load_manifest

_SEPARATION_OF_DUTIES_EXPLANATION = (
    "\n\nEsto NO es un fallo a corregir en este test: es una violación de la regla de "
    "Separation of Duties del motor de stacks. La red base (VPCs, subnets, security "
    "groups) la administra Networking/SecOps, no un manifiesto de stack -- ver "
    "docs/least-privilege.md, sección 'Separation of Duties'. Un stack REFERENCIA la "
    "red existente por nombre (vía NetworkResolver, de solo lectura); nunca la crea."
)

_NETWORK_PREFIXES = ("vpc:", "subnet:", "security-group:")

_FORBIDDEN_NETWORK_MUTATION_RE_PARTS = (
    "create_security_group",
    "authorize_security_group",
    "create_subnet",
    "create_vpc",
    "modify_subnet",
    "revoke_security_group",
)

_STACKS_DIR = Path(__file__).parents[3] / "src" / "aws_admin_cli" / "application" / "stacks"


def test_resource_kind_has_no_network_member() -> None:
    offenders = [kind for kind in ResourceKind if kind.value.startswith(_NETWORK_PREFIXES)]
    assert not offenders, (
        f"ResourceKind tiene miembro(s) de red: {offenders}." + _SEPARATION_OF_DUTIES_EXPLANATION
    )


def test_stacks_module_source_has_no_network_mutation_calls() -> None:
    sources: dict[str, str] = {}
    for path in _STACKS_DIR.rglob("*.py"):
        sources[str(path.relative_to(_STACKS_DIR.parents[3]))] = path.read_text(encoding="utf-8")

    assert sources, "No se encontró ningún archivo bajo application/stacks/ -- ¿se movió?"

    for label, source in sources.items():
        offenders = [needle for needle in _FORBIDDEN_NETWORK_MUTATION_RE_PARTS if needle in source]
        assert not offenders, (
            f"{label} contiene referencia(s) a mutación de red prohibida(s): {offenders}."
            + _SEPARATION_OF_DUTIES_EXPLANATION
        )


def test_stack_step_protocol_exposes_no_network_resource_kind() -> None:
    """No concrete step's ``kind`` (declared or default) is a network kind.

    Belt-and-suspenders alongside the ``ResourceKind`` check above: even if
    someone widened ``ResourceKind`` AND wired a step for it, this would
    still catch a step whose class-level ``kind`` attribute resolves to one.
    """
    from aws_admin_cli.application.stacks import registry

    module_source = inspect.getsource(registry)
    for prefix in _NETWORK_PREFIXES:
        assert f'"{prefix}' not in module_source and f"'{prefix}" not in module_source, (
            f"registry.py referencia un kind de red ('{prefix}...')."
            + _SEPARATION_OF_DUTIES_EXPLANATION
        )


def test_a_manifest_declaring_a_network_kind_is_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "network-stack.yaml"
    manifest_path.write_text(
        "apiVersion: v1\n"
        "name: demo-network\n"
        "resources:\n"
        "  - id: extra-sg\n"
        "    kind: security-group:sg\n"
        "    properties:\n"
        "      group_name: demo-extra-sg\n"
    )

    with pytest.raises(ValidationError) as exc_info:
        load_manifest(manifest_path)

    message = str(exc_info.value) + str(exc_info.value.hint)
    assert "Separation of Duties" in message or "Networking/SecOps" in message
    assert exc_info.value.exit_code == 64
