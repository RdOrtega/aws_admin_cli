"""Shared fixtures for e2e tests: a throwaway AWS profile + a LocalStack reachability skip."""

import socket
from pathlib import Path

import pytest

LOCALSTACK_HOST = "localhost"
LOCALSTACK_PORT = 4566
LOCALSTACK_ENDPOINT = f"http://{LOCALSTACK_HOST}:{LOCALSTACK_PORT}"


def localstack_is_up() -> bool:
    """Whether something is listening on LocalStack's usual host:port."""
    try:
        with socket.create_connection((LOCALSTACK_HOST, LOCALSTACK_PORT), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _fake_aws_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LocalStack accepts any static credentials; point boto3 at a throwaway profile."""
    config_file = tmp_path / "aws-config"
    credentials_file = tmp_path / "aws-credentials"
    config_file.write_text("[profile testprofile]\nregion = us-east-1\n")
    credentials_file.write_text(
        "[testprofile]\naws_access_key_id = test\naws_secret_access_key = test\n"
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_file))


@pytest.fixture
def skip_if_localstack_down() -> None:
    """Skip the test if nothing is listening on LocalStack's usual host:port."""
    if not localstack_is_up():
        pytest.skip(f"LocalStack no responde en {LOCALSTACK_HOST}:{LOCALSTACK_PORT}.")
