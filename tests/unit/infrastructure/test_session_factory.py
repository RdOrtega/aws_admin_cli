"""Tests for Boto3SessionFactory: profile/credential error translation and caching."""

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from aws_admin_cli.core.exceptions import MissingCredentialsError, ProfileNotFoundError
from aws_admin_cli.infrastructure.aws import session_factory as session_factory_module
from aws_admin_cli.infrastructure.aws.session_factory import Boto3SessionFactory
from botocore.exceptions import ProfileNotFound
from pytest_mock import MockerFixture

_TARGET = "aws_admin_cli.infrastructure.aws.session_factory.boto3.session.Session"


@pytest.fixture(autouse=True)
def _clear_session_cache() -> Iterator[None]:
    """The session cache is module-level; reset it so tests don't leak into each other."""
    session_factory_module._session_cache.clear()
    yield
    session_factory_module._session_cache.clear()


def test_profile_not_found_translates_and_lists_available(mocker: MockerFixture) -> None:
    def session_side_effect(*_args: object, **kwargs: object) -> MagicMock:
        if kwargs.get("profile_name") == "missing":
            raise ProfileNotFound(profile="missing")
        session = MagicMock()
        session.available_profiles = ["default", "localstack"]
        return session

    mocker.patch(_TARGET, side_effect=session_side_effect)

    factory = Boto3SessionFactory(profile="missing", region="us-east-1")

    with pytest.raises(ProfileNotFoundError) as exc_info:
        factory.get_session()

    assert exc_info.value.hint is not None
    assert "default" in exc_info.value.hint
    assert "localstack" in exc_info.value.hint


def test_get_session_is_cached(mocker: MockerFixture) -> None:
    session = MagicMock()
    session.get_credentials.return_value = MagicMock()
    session_ctor = mocker.patch(_TARGET, return_value=session)

    factory = Boto3SessionFactory(profile="localstack", region="us-east-1")

    first = factory.get_session()
    second = factory.get_session()

    assert first is second
    session_ctor.assert_called_once()


def test_missing_credentials_raise_missing_credentials_error(mocker: MockerFixture) -> None:
    session = MagicMock()
    session.get_credentials.return_value = None
    mocker.patch(_TARGET, return_value=session)

    factory = Boto3SessionFactory(profile="localstack", region="us-east-1")

    with pytest.raises(MissingCredentialsError):
        factory.get_session()


def test_local_endpoint_bypasses_the_profile_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loopback endpoint must never touch ~/.aws, even with a nonexistent profile."""
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    factory = Boto3SessionFactory(
        profile="perfil-que-no-existe",
        region="us-east-1",
        endpoint_url="http://localhost:4566",
    )
    credentials = factory.get_session().get_credentials()

    assert credentials is not None
    assert credentials.access_key == "test"


def test_exported_credentials_win_over_the_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "desde-el-entorno")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secreto")

    factory = Boto3SessionFactory(
        profile="localstack",
        region="eu-west-1",
        endpoint_url="http://127.0.0.1:4566",
    )
    credentials = factory.get_session().get_credentials()

    assert credentials is not None
    assert credentials.access_key == "desde-el-entorno"


def test_remote_endpoint_still_resolves_the_profile_normally() -> None:
    """A non-loopback endpoint is not an emulator: the profile must still be honored."""
    factory = Boto3SessionFactory(
        profile="perfil-que-no-existe",
        region="us-east-1",
        endpoint_url="https://vpce-1234.s3.us-east-1.vpce.amazonaws.com",
    )
    with pytest.raises(ProfileNotFoundError):
        factory.get_session()
