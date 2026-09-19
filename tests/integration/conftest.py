"""Shared fixtures for integration tests: a throwaway AWS profile + a moto guardrail."""

from pathlib import Path

import pytest
from aws_admin_cli.core.config import Settings


@pytest.fixture(autouse=True)
def _fake_aws_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point boto3 at a throwaway profile file, and guard against a leaked endpoint_url.

    ``Boto3SessionFactory`` requires the configured profile to actually exist
    locally (boto3 raises ``ProfileNotFound`` for any named profile that isn't
    in ``~/.aws/config``, even on a machine with no AWS config at all) -- so a
    real, if disposable, profile is needed here rather than mocking that away,
    to exercise the CLI's actual session-construction path.

    Also asserts ``settings.endpoint_url is None``: moto only intercepts real
    AWS endpoints. If a developer's local ``.env`` ever leaked
    ``AWS_ADMIN_CLI_ENDPOINT_URL`` into these tests (the global
    ``isolated_env`` fixture should already prevent this, but this is a
    "trust, and verify" second guardrail), boto3 would target LocalStack
    instead, moto would never see the request, and every integration test
    would fail confusingly -- or worse, hang or pass for the wrong reason.
    """
    config_file = tmp_path / "aws-config"
    credentials_file = tmp_path / "aws-credentials"
    config_file.write_text("[profile testprofile]\nregion = us-east-1\n")
    credentials_file.write_text(
        "[testprofile]\naws_access_key_id = testing\naws_secret_access_key = testing\n"
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_file))

    settings = Settings(profile="testprofile")
    assert settings.endpoint_url is None, (
        "settings.endpoint_url no es None en un test de integración: revisa que "
        "AWS_ADMIN_CLI_ENDPOINT_URL no esté filtrando desde el entorno o un .env -- "
        "moto no intercepta llamadas dirigidas a LocalStack."
    )
