"""Enforces the ``vpc`` module's Separation of Duties: it must never mutate network state.

The base network (VPCs, subnets, security groups, route tables, internet/NAT
gateways) belongs to Networking/SecOps, not to this tool -- see
``docs/least-privilege.md``'s "Separation of Duties" section for the reasoning.
Every check in this file exists to turn "someone accidentally added a write
method to the vpc module" into a failing test, not a missed code-review
comment. If you're reading this because a test below just failed you: that is
the point. The fix is almost never to adjust this test -- it's to move the
mutating operation you just added out of ``domain/ports/vpc_gateway.py``,
``infrastructure/aws/gateways/boto3_vpc_gateway.py``, and
``presentation/cli/vpc_app.py`` entirely. Networking/SecOps owns that
mutation, not this CLI.
"""

import inspect
import re
from pathlib import Path

from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway
from aws_admin_cli.infrastructure.aws.gateways.boto3_vpc_gateway import Boto3VpcGateway

_FORBIDDEN_PREFIX_RE = re.compile(
    r"^(create|delete|modify|authorize|revoke|associate|disassociate|attach|detach|"
    r"replace|update|put|run|terminate|start|stop|reboot)_"
)
_REQUIRED_PREFIX_RE = re.compile(r"^describe_")

_FORBIDDEN_CLIENT_CALL_RE = re.compile(
    r"client\s*\(\s*\)\s*\.\s*(create|delete|authorize|revoke|modify)_\w*"
    r"|client\s*\.\s*(create|delete|authorize|revoke|modify)_\w*"
)

_SEPARATION_OF_DUTIES_EXPLANATION = (
    "\n\nEsto NO es un fallo a corregir en este test: es una violación de la regla de "
    "Separation of Duties del módulo vpc (ver docs/least-privilege.md, sección "
    "'Separation of Duties'). La red base (VPCs, subnets, security groups, route "
    "tables, internet/NAT gateways) la administra Networking/SecOps, no esta CLI. "
    "Si acabas de añadir un método de mutación aquí, muévelo fuera del módulo vpc -- "
    "no lo implementes en el Protocol, en Boto3VpcGateway, ni en la CLI de vpc."
)

_GATEWAY_SOURCE_PATH = (
    Path(__file__).parents[3]
    / "src"
    / "aws_admin_cli"
    / "infrastructure"
    / "aws"
    / "gateways"
    / "boto3_vpc_gateway.py"
)
_CLI_SOURCE_PATH = (
    Path(__file__).parents[3] / "src" / "aws_admin_cli" / "presentation" / "cli" / "vpc_app.py"
)

# EC2 (Fase 5) legitimately calls plenty of "create_"/"start_"/"stop_"/"terminate_"
# operations -- on INSTANCES, key pairs, and AMIs. What it must never do is create,
# modify, or authorize network resources (VPCs, subnets, security groups): EC2
# CONSUMES the network via NetworkResolver (read-only), it never administers it.
_FORBIDDEN_NETWORK_MUTATION_RE = re.compile(
    r"create_security_group|authorize_security_group|create_subnet|create_vpc|"
    r"modify_subnet|revoke_security_group"
)
_NETWORK_RESOURCE_WORDS = ("security_group", "subnet", "vpc")
_MUTATING_VERB_RE = re.compile(
    r"^(create|delete|modify|authorize|revoke|associate|disassociate|attach|detach|"
    r"replace|update|put)_"
)

_EC2_GATEWAY_SOURCE_PATH = (
    Path(__file__).parents[3]
    / "src"
    / "aws_admin_cli"
    / "infrastructure"
    / "aws"
    / "gateways"
    / "boto3_ec2_gateway.py"
)
_EC2_CLI_SOURCE_PATH = (
    Path(__file__).parents[3] / "src" / "aws_admin_cli" / "presentation" / "cli" / "ec2_app.py"
)
_EC2_USE_CASES_DIR = (
    Path(__file__).parents[3] / "src" / "aws_admin_cli" / "application" / "use_cases" / "ec2"
)


def _public_method_names(cls: type) -> list[str]:
    return [
        name
        for name, _member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]


def test_vpc_gateway_protocol_has_no_forbidden_mutating_methods() -> None:
    names = _public_method_names(VpcGateway)
    offenders = [name for name in names if _FORBIDDEN_PREFIX_RE.match(name)]
    assert not offenders, (
        f"VpcGateway (el Protocol) expone método(s) de mutación: {offenders}."
        + _SEPARATION_OF_DUTIES_EXPLANATION
    )


def test_boto3_vpc_gateway_has_no_forbidden_mutating_methods() -> None:
    names = _public_method_names(Boto3VpcGateway)
    offenders = [name for name in names if _FORBIDDEN_PREFIX_RE.match(name)]
    assert not offenders, (
        f"Boto3VpcGateway expone método(s) de mutación: {offenders}."
        + _SEPARATION_OF_DUTIES_EXPLANATION
    )


def test_vpc_gateway_protocol_methods_are_all_describe_only() -> None:
    names = _public_method_names(VpcGateway)
    assert names, "VpcGateway no expone ningún método público -- ¿se rompió la definición?"
    offenders = [name for name in names if not _REQUIRED_PREFIX_RE.match(name)]
    assert not offenders, (
        f"VpcGateway expone método(s) que no empiezan por 'describe_': {offenders}. "
        "El puerto vpc es exclusivamente de consulta." + _SEPARATION_OF_DUTIES_EXPLANATION
    )


def test_boto3_vpc_gateway_source_has_no_forbidden_client_calls() -> None:
    source = _GATEWAY_SOURCE_PATH.read_text(encoding="utf-8")
    offenders = _FORBIDDEN_CLIENT_CALL_RE.findall(source)
    assert not offenders, (
        f"boto3_vpc_gateway.py contiene llamada(s) de cliente prohibidas: {offenders}."
        + _SEPARATION_OF_DUTIES_EXPLANATION
    )


def test_vpc_cli_source_has_no_forbidden_client_calls() -> None:
    source = _CLI_SOURCE_PATH.read_text(encoding="utf-8")
    offenders = _FORBIDDEN_CLIENT_CALL_RE.findall(source)
    assert not offenders, (
        f"vpc_app.py contiene llamada(s) de cliente prohibidas: {offenders}."
        + _SEPARATION_OF_DUTIES_EXPLANATION
    )


def test_ec2_gateway_protocol_exposes_no_network_mutating_methods() -> None:
    names = _public_method_names(Ec2Gateway)
    offenders = [
        name
        for name in names
        if _MUTATING_VERB_RE.match(name) and any(word in name for word in _NETWORK_RESOURCE_WORDS)
    ]
    assert not offenders, (
        f"Ec2Gateway expone método(s) de mutación de red: {offenders}."
        + _SEPARATION_OF_DUTIES_EXPLANATION
    )


def test_ec2_module_source_has_no_network_mutation_calls() -> None:
    sources = {
        "boto3_ec2_gateway.py": _EC2_GATEWAY_SOURCE_PATH.read_text(encoding="utf-8"),
        "ec2_app.py": _EC2_CLI_SOURCE_PATH.read_text(encoding="utf-8"),
    }
    for path in sorted(_EC2_USE_CASES_DIR.glob("*.py")):
        sources[f"use_cases/ec2/{path.name}"] = path.read_text(encoding="utf-8")

    for label, source in sources.items():
        offenders = _FORBIDDEN_NETWORK_MUTATION_RE.findall(source)
        assert not offenders, (
            f"{label} contiene llamada(s) de mutación de red prohibidas: {offenders}."
            + _SEPARATION_OF_DUTIES_EXPLANATION
        )
