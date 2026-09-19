"""Tests for the EC2 launch/lifecycle guard rails."""

from datetime import UTC, datetime

import pytest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.ec2 import Instance, InstanceState
from aws_admin_cli.domain.policies.launch_rules import (
    DEFAULT_ALLOWED_FAMILIES,
    check_instance_type,
    check_managed_tag,
    check_public_ip,
)


def _instance(*, tags: list[dict[str, str]] | None = None) -> Instance:
    return Instance(
        instance_id="i-0a1b2c3d4e5f6a7b8",
        instance_type="t3.micro",
        state=InstanceState.RUNNING,
        image_id="ami-0a1b2c3d",
        launch_time=datetime.now(UTC),
        tags=tags if tags is not None else [],
    )


# -- check_instance_type -------------------------------------------------------------


def test_check_instance_type_allowed_family_does_not_raise() -> None:
    check_instance_type("t3.micro", DEFAULT_ALLOWED_FAMILIES, confirmed=False)


def test_check_instance_type_disallowed_family_without_confirm_raises_naming_the_family() -> None:
    # r5 is deliberately NOT in DEFAULT_ALLOWED_FAMILIES (t2/t3/t3a/m5/m6i) --
    # unlike m5, which IS allowed even at a large size (this rule is family-based
    # only, not size-based; see check_instance_type's docstring).
    with pytest.raises(ValidationError) as exc_info:
        check_instance_type("r5.24xlarge", DEFAULT_ALLOWED_FAMILIES, confirmed=False)
    assert "r5" in str(exc_info.value)


def test_check_instance_type_disallowed_family_with_confirm_does_not_raise() -> None:
    check_instance_type("r5.24xlarge", DEFAULT_ALLOWED_FAMILIES, confirmed=True)


def test_check_instance_type_m5_is_allowed_even_at_a_large_size() -> None:
    # DEFAULT_ALLOWED_FAMILIES includes "m5" -- this check is family-based, not
    # size-based, so even a large m5 size never requires --confirm-large.
    check_instance_type("m5.24xlarge", DEFAULT_ALLOWED_FAMILIES, confirmed=False)


# -- check_public_ip ------------------------------------------------------------------


def test_check_public_ip_not_requested_never_raises() -> None:
    check_public_ip(False, None, confirmed=False)
    check_public_ip(False, False, confirmed=False)
    check_public_ip(False, True, confirmed=False)


def test_check_public_ip_into_a_known_private_subnet_raises() -> None:
    with pytest.raises(ValidationError):
        check_public_ip(True, False, confirmed=True)


def test_check_public_ip_into_a_public_subnet_confirmed_does_not_raise() -> None:
    check_public_ip(True, True, confirmed=True)


def test_check_public_ip_without_confirm_raises() -> None:
    with pytest.raises(ValidationError):
        check_public_ip(True, True, confirmed=False)


def test_check_public_ip_unknown_visibility_confirmed_is_allowed() -> None:
    # is_public=None (couldn't be determined) never blocks by itself -- the
    # calling use case is responsible for logging that uncertainty.
    check_public_ip(True, None, confirmed=True)


# -- check_managed_tag ----------------------------------------------------------------


def test_check_managed_tag_with_the_tag_does_not_raise() -> None:
    instance = _instance(tags=[{"Key": "ManagedBy", "Value": "aws-admin-cli"}])
    check_managed_tag(instance, force=False)


def test_check_managed_tag_without_the_tag_and_no_force_raises() -> None:
    instance = _instance(tags=[])
    with pytest.raises(ValidationError):
        check_managed_tag(instance, force=False)


def test_check_managed_tag_without_the_tag_and_force_does_not_raise() -> None:
    instance = _instance(tags=[])
    check_managed_tag(instance, force=True)
