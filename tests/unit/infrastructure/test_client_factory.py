"""Tests for ClientFactory: the endpoint_url single-decision-point guardian."""

from unittest.mock import MagicMock

from aws_admin_cli import __version__
from aws_admin_cli.core.config import Settings
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory

from tests.conftest import FakeSessionFactory


def test_client_kwargs_include_endpoint_url_when_set(
    settings_local: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_local)

    kwargs = factory._client_kwargs("iam")

    assert kwargs["endpoint_url"] == "http://localhost:4566"


def test_client_kwargs_omit_endpoint_url_when_unset(
    settings_real: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_real)

    kwargs = factory._client_kwargs("iam")

    assert "endpoint_url" not in kwargs


def test_botocore_config_carries_user_agent_extra(
    settings_real: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_real)

    config = factory._botocore_config("iam")

    assert config.user_agent_extra == f"aws-admin-cli/{__version__}"


def test_create_caches_client_by_service_name(
    settings_real: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_real)

    first = factory.create("s3")
    second = factory.create("s3")

    assert first is second
    fake_session_factory.session.client.assert_called_once()


def test_clear_cache_forces_the_next_create_to_build_a_fresh_client(
    settings_real: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    """Simulates a region switch: the client built under the old region must be
    dropped, not handed back stale, once ``clear_cache()`` runs.
    """
    fake_session_factory.session.client.side_effect = lambda *_args, **_kwargs: MagicMock()
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_real)

    before = factory.create("s3")
    factory.clear_cache()
    after = factory.create("s3")

    assert before is not after
    assert fake_session_factory.session.client.call_count == 2


# -- Per-service config overrides (_SERVICE_CONFIG_OVERRIDES) --------------------


def test_s3_config_forces_path_addressing_when_endpoint_url_set(
    settings_local: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_local)

    config = factory._botocore_config("s3")

    assert config.s3["addressing_style"] == "path"


def test_s3_config_does_not_force_addressing_without_endpoint_url(
    settings_real: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_real)

    config = factory._botocore_config("s3")

    assert config.s3 is None


def test_iam_config_is_unaffected_by_the_s3_override(
    settings_local: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_local)

    config = factory._botocore_config("iam")

    assert config.s3 is None


# -- Global Region Context propagation -------------------------------------------


def test_botocore_config_region_name_reflects_settings_region(
    settings_real: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_real)

    assert factory._botocore_config("ec2").region_name == "us-east-1"


def test_mutating_settings_region_in_place_is_reflected_in_every_services_config(
    settings_real: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    """Simulates ``AppContext.set_region``: mutating ``settings.region`` in place (the
    Region Selector's own mechanism) must be picked up by every gateway's client config,
    not just whichever service happens to build a client first.
    """
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_real)
    object.__setattr__(settings_real, "region", "eu-west-1")

    assert factory._botocore_config("ec2").region_name == "eu-west-1"
    assert factory._botocore_config("s3").region_name == "eu-west-1"
    assert factory._botocore_config("cloudwatch").region_name == "eu-west-1"
    assert factory._botocore_config("iam").region_name == "eu-west-1"


def test_create_passes_the_current_region_name_to_session_client(
    settings_real: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_real)

    factory.create("ec2")

    _, kwargs = fake_session_factory.session.client.call_args
    assert kwargs["config"].region_name == "us-east-1"


def test_clear_cache_then_create_after_a_region_switch_uses_the_new_region_name(
    settings_real: Settings, fake_session_factory: FakeSessionFactory
) -> None:
    """The full ``AppContext.set_region`` sequence: mutate ``settings.region`` in place,
    then ``clear_cache()`` -- the next client built for ANY service must carry the new
    ``region_name``, with no fresh ``ClientFactory``/``AppContext`` required.
    """
    fake_session_factory.session.client.side_effect = lambda *_a, **_k: MagicMock()
    factory = ClientFactory(session_factory=fake_session_factory, settings=settings_real)
    factory.create("ec2")

    object.__setattr__(settings_real, "region", "eu-west-1")
    factory.clear_cache()
    factory.create("ec2")

    _, kwargs = fake_session_factory.session.client.call_args
    assert kwargs["config"].region_name == "eu-west-1"
