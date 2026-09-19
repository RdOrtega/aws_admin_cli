"""E2E test: `vpc` module against the network seeded by localstack/init/01-bootstrap.sh.

Requires `make up` (which runs the bootstrap on first boot) or `make seed`
(which re-runs it against an already-running LocalStack). Never fails just
because the network hasn't been seeded -- it skips with a clear message
instead, since "LocalStack is up but nobody ran the bootstrap yet" is an
environment-setup gap, not a test failure.
"""

import json

import pytest
from aws_admin_cli.main import app
from typer.testing import CliRunner

from tests.e2e.conftest import LOCALSTACK_ENDPOINT

_BASE_ARGS = ["--profile", "testprofile", "--endpoint-url", LOCALSTACK_ENDPOINT]


@pytest.fixture
def skip_if_bootstrap_not_seeded(cli_runner: CliRunner) -> None:
    """Skip (never fail) if the bootstrap network isn't present in this LocalStack."""
    result = cli_runner.invoke(app, [*_BASE_ARGS, "--output", "json", "vpc", "list"])
    if result.exit_code != 0:
        pytest.skip(f"No se pudo listar VPCs contra LocalStack: {result.output}")
    vpcs = json.loads(result.stdout)
    if not any(vpc.get("Tags") and _has_name_tag(vpc, "corp-main-vpc") for vpc in vpcs):
        pytest.skip(
            "No se encontró la VPC 'corp-main-vpc' -- ejecuta `make seed` para sembrar "
            "la red de prueba antes de correr los tests e2e de vpc."
        )


def _has_name_tag(vpc: dict[str, object], name: str) -> bool:
    tags = vpc.get("Tags")
    if not isinstance(tags, list):
        return False
    return any(tag.get("Key") == "Name" and tag.get("Value") == name for tag in tags)


@pytest.mark.e2e
@pytest.mark.usefixtures("skip_if_localstack_down", "skip_if_bootstrap_not_seeded")
def test_vpc_list_finds_the_seeded_corp_main_vpc(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, [*_BASE_ARGS, "--output", "json", "vpc", "list"])

    assert result.exit_code == 0, result.output
    vpcs = json.loads(result.stdout)
    assert any(_has_name_tag(vpc, "corp-main-vpc") for vpc in vpcs)


@pytest.mark.e2e
@pytest.mark.usefixtures("skip_if_localstack_down", "skip_if_bootstrap_not_seeded")
def test_sg_audit_detects_the_three_planted_findings(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(
        app, [*_BASE_ARGS, "--output", "json", "vpc", "sg", "audit", "--vpc", "corp-main-vpc"]
    )

    assert result.exit_code == 0, result.output
    findings = json.loads(result.stdout)
    rule_ids = {finding["RuleId"] for finding in findings}

    # The three findings 01-bootstrap.sh deliberately plants:
    # corp-bastion-sg (SSH), corp-db-sg (PostgreSQL), corp-legacy-sg (wide range).
    assert "SG001" in rule_ids  # bastion: SSH open to the world
    assert "SG004" in rule_ids  # db: a database port open to the world
    assert "SG005" in rule_ids  # legacy: a wide port range open to the world


@pytest.mark.e2e
@pytest.mark.usefixtures("skip_if_localstack_down", "skip_if_bootstrap_not_seeded")
def test_vpc_resolve_subnet_returns_a_valid_subnet_id(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(
        app,
        [
            *_BASE_ARGS,
            "--output",
            "json",
            "vpc",
            "resolve",
            "--vpc",
            "corp-main-vpc",
            "--subnet",
            "corp-private-1a",
        ],
    )

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    resolved = next(row for row in rows if row["Type"] == "subnet")
    assert resolved["ResolvedId"].startswith("subnet-")
