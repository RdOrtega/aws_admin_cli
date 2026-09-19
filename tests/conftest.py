"""Shared pytest fixtures: fake AWS credentials guard, CLI runner, and settings doubles."""

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aws_admin_cli.core.config import Settings
from typer.testing import CliRunner


@pytest.fixture(autouse=True, scope="session")
def _fake_aws_credentials() -> Iterator[None]:
    """Force fake AWS credentials for the whole test session.

    Guarantees that no test can ever reach a real AWS account, regardless of what
    is configured on the machine running the suite.
    """
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ.pop("AWS_PROFILE", None)
    yield


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Strip ``AWS_ADMIN_CLI_*`` from the environment and hide any real ``.env``.

    Runs before every test (autouse) so ``Settings()`` never picks up a
    developer's local profile/region/endpoint overrides: known
    ``AWS_ADMIN_CLI_*`` variables are unset, and the process is chdir'd into an
    empty ``tmp_path`` so pydantic-settings' relative ``.env`` lookup can never
    find the repo's own (gitignored, developer-local) ``.env`` file either.
    """
    for key in list(os.environ):
        if key.startswith("AWS_ADMIN_CLI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    # Settings.data_dir defaults to Path.home() / ".aws_admin_cli" -- without this,
    # any test that builds a real Settings() and touches its resource_repository
    # would read/write the developer's actual home directory. Route it into the
    # same disposable tmp_path instead.
    monkeypatch.setenv("AWS_ADMIN_CLI_DATA_DIR", str(tmp_path / "aws_admin_cli_data"))


@pytest.fixture(autouse=True)
def _reset_app_logger_propagation() -> Iterator[None]:
    """Reset the ``aws_admin_cli`` logger's ``propagate`` flag before every test.

    ``core.logging.configure_logging`` (run by every real CLI invocation, via
    ``AppContext.build``) sets ``propagate = False`` on this logger so its
    Rich handler is the sole output -- but that logger is a process-wide
    singleton, so once any test exercises a real CLI command, every *later*
    test using ``caplog`` (which only captures what propagates to the root
    logger) would silently stop seeing anything from it. Restoring the
    default here keeps `caplog` reliable regardless of test order.
    """
    logger = logging.getLogger("aws_admin_cli")
    original = logger.propagate
    logger.propagate = True
    yield
    logger.propagate = original


@pytest.fixture
def cli_runner() -> CliRunner:
    """Return a Typer CLI test runner."""
    return CliRunner()


@pytest.fixture
def settings_local() -> Settings:
    """``Settings`` targeting a LocalStack endpoint."""
    return Settings(endpoint_url="http://localhost:4566")


@pytest.fixture
def settings_real() -> Settings:
    """``Settings`` targeting real AWS (no endpoint override).

    Explicit non-local profile: the default profile is ``"localstack"``, which
    (per ``core.config._LOCAL_PROFILE_ENDPOINTS``) gets its endpoint injected
    automatically -- using the bare default here would silently make this
    fixture target LocalStack instead of "real AWS, no endpoint set".
    """
    return Settings(profile="production")


@dataclass
class FakeSessionFactory:
    """A ``SessionFactory`` test double: ``get_session()`` returns a canned fake session.

    Satisfies the ``SessionFactory`` protocol structurally (duck typing), so it
    can stand in anywhere a real ``Boto3SessionFactory`` is expected, with no
    network or credentials involved.
    """

    session: Any = field(default_factory=MagicMock)

    def get_session(self) -> Any:
        """Return the canned fake session."""
        return self.session


@pytest.fixture
def fake_session_factory() -> FakeSessionFactory:
    """A ``SessionFactory`` double whose ``get_session().client(...)`` is a ``MagicMock``."""
    return FakeSessionFactory()
