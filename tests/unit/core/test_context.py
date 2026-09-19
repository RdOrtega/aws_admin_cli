"""Tests for ``AppContext.set_region``: the session-wide region switch."""

from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext


def test_set_region_updates_settings_region() -> None:
    ctx = AppContext.build(Settings(profile="localstack", region="us-east-1"))

    ctx.set_region("eu-west-1")

    assert ctx.settings.region == "eu-west-1"


def test_set_region_is_visible_through_the_client_factorys_own_settings_reference() -> None:
    """``client_factory.settings`` is the exact same ``Settings`` instance as
    ``ctx.settings`` -- gateways read region off the former, so the switch must
    be visible there too, not just on ``ctx.settings`` itself.
    """
    ctx = AppContext.build(Settings(profile="localstack", region="us-east-1"))

    ctx.set_region("sa-east-1")

    assert ctx.client_factory.settings.region == "sa-east-1"


def test_set_region_clears_the_client_factory_cache() -> None:
    ctx = AppContext.build(Settings(profile="localstack", region="us-east-1"))
    ctx.client_factory._client_cache["s3"] = object()

    ctx.set_region("eu-west-1")

    assert ctx.client_factory._client_cache == {}
