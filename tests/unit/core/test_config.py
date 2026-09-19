"""Tests for Settings: defaults, precedence, validation, and immutability."""

from pathlib import Path

import pytest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.enums import LogLevel, OutputFormat
from aws_admin_cli.core.exceptions import ConfigurationError


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # isolated_env (conftest.py, autouse) always sets AWS_ADMIN_CLI_DATA_DIR to a
    # disposable tmp_path, precisely so no test ever touches the real ~/.aws_admin_cli
    # -- unset it just for this one assertion, to verify the class-level default itself.
    monkeypatch.delenv("AWS_ADMIN_CLI_DATA_DIR", raising=False)
    settings = Settings()

    assert settings.profile == "localstack"
    assert settings.region == "us-east-1"
    # The 'localstack' profile is registered in _LOCAL_PROFILE_ENDPOINTS, so it gets
    # its emulator endpoint injected when none was supplied.
    assert settings.endpoint_url == "http://localhost:4566"
    assert settings.output is OutputFormat.TABLE
    assert settings.log_level is LogLevel.INFO
    assert settings.data_dir == Path.home() / ".aws_admin_cli"
    assert settings.max_attempts == 3
    assert settings.retry_mode == "standard"
    assert settings.connect_timeout == 10
    assert settings.read_timeout == 30
    assert settings.is_local is True


def test_env_var_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ADMIN_CLI_PROFILE", "prod")

    assert Settings().profile == "prod"


def test_with_overrides_applies_only_non_none_fields() -> None:
    original = Settings()

    overridden = original.with_overrides(profile="x", region=None)

    assert overridden.profile == "x"
    assert overridden.region == original.region  # untouched by the None override


def test_with_overrides_returns_new_instance_and_leaves_original_intact() -> None:
    original = Settings()

    overridden = original.with_overrides(profile="something-else")

    assert overridden is not original
    assert overridden.profile == "something-else"
    assert original.profile == "localstack"


def test_settings_is_frozen() -> None:
    settings = Settings()

    with pytest.raises(Exception):  # noqa: B017 -- pydantic raises its own ValidationError
        settings.profile = "mutated"  # type: ignore[misc]


def test_blank_endpoint_url_normalizes_to_none() -> None:
    # Blank -> None, and then the local-profile default fills the gap for 'localstack'.
    assert Settings(endpoint_url="", profile="default").endpoint_url is None
    assert Settings(endpoint_url="", profile="localstack").endpoint_url == (
        "http://localhost:4566"
    )


def test_local_profile_gets_its_endpoint_injected() -> None:
    assert Settings(profile="localstack").endpoint_url == "http://localhost:4566"


def test_non_local_profile_gets_no_endpoint() -> None:
    assert Settings(profile="production").endpoint_url is None


def test_explicit_endpoint_url_is_never_overwritten() -> None:
    settings = Settings(profile="localstack", endpoint_url="http://localhost:9999")
    assert settings.endpoint_url == "http://localhost:9999"


def test_local_endpoint_injection_survives_with_overrides() -> None:
    settings = Settings(profile="production").with_overrides(profile="localstack")
    assert settings.endpoint_url == "http://localhost:4566"


def test_invalid_endpoint_url_scheme_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        Settings(endpoint_url="ftp://x")


def test_invalid_region_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        Settings(region="INVALIDA")


@pytest.mark.parametrize(
    ("dotenv_value", "env_value", "override_value", "expected"),
    [
        (None, None, None, "localstack"),
        ("dotenv-profile", None, None, "dotenv-profile"),
        ("dotenv-profile", "env-profile", None, "env-profile"),
        ("dotenv-profile", "env-profile", "override-profile", "override-profile"),
    ],
    ids=["default-only", "dotenv-over-default", "env-over-dotenv", "override-over-env"],
)
def test_precedence_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dotenv_value: str | None,
    env_value: str | None,
    override_value: str | None,
    expected: str,
) -> None:
    if dotenv_value is not None:
        (tmp_path / ".env").write_text(f"AWS_ADMIN_CLI_PROFILE={dotenv_value}\n")
    if env_value is not None:
        monkeypatch.setenv("AWS_ADMIN_CLI_PROFILE", env_value)

    settings = Settings()
    if override_value is not None:
        settings = settings.with_overrides(profile=override_value)

    assert settings.profile == expected
