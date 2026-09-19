"""E2E test: `doctor` against a real, locally running LocalStack (requires `make up`)."""

import pytest
from aws_admin_cli.main import app
from typer.testing import CliRunner

from tests.e2e.conftest import LOCALSTACK_ENDPOINT


@pytest.mark.e2e
@pytest.mark.usefixtures("skip_if_localstack_down")
def test_doctor_against_localstack(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(
        app, ["--profile", "testprofile", "--endpoint-url", LOCALSTACK_ENDPOINT, "doctor"]
    )

    assert result.exit_code == 0, result.output
