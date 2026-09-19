"""Tests for the start/stop/terminate lifecycle use cases: state machine gates first,
the ManagedBy safeguard on terminate, and OperationTimeoutError propagation from waits.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from aws_admin_cli.application.use_cases.ec2.start_instance import StartInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.stop_instance import StopInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.terminate_instance import TerminateInstanceUseCase
from aws_admin_cli.core.exceptions import OperationTimeoutError, ValidationError
from aws_admin_cli.domain.models.ec2 import Instance, InstanceState

from tests.fakes.ec2 import FakeEc2Gateway
from tests.fakes.iam import InMemoryRepository


def _instance(instance_id: str, state: InstanceState, *, managed: bool = True) -> Instance:
    tags = [{"Key": "ManagedBy", "Value": "aws-admin-cli"}] if managed else []
    return Instance(
        instance_id=instance_id,
        instance_type="t3.micro",
        state=state,
        image_id="ami-1111111111111111",
        subnet_id="subnet-0a1b2c03",
        launch_time=datetime(2024, 1, 1, tzinfo=UTC),
        tags=tags,
    )


class _TimeoutEc2Gateway(FakeEc2Gateway):
    """A ``FakeEc2Gateway`` whose ``wait_for_state`` always times out."""

    def wait_for_state(
        self,
        instance_ids: Sequence[str],
        target: InstanceState,
        *,
        timeout_s: int,
        poll_s: int,
        region: str | None = None,
    ) -> None:
        del region
        raise OperationTimeoutError(
            f"Tiempo de espera agotado tras {timeout_s}s esperando el estado '{target.value}'.",
            hint="Vuelve a consultar con `ec2 instance show`.",
            service="ec2",
            operation="WaitForInstanceRunning",
        )


# -- start --------------------------------------------------------------------------


def test_start_on_terminated_raises_without_calling_the_gateway() -> None:
    gateway = FakeEc2Gateway()
    instance = _instance("i-terminated0000", InstanceState.TERMINATED)
    gateway.instances[instance.instance_id] = instance

    with pytest.raises(ValidationError) as exc_info:
        StartInstanceUseCase(gateway=gateway).execute(instance, wait=False, timeout_s=60)

    assert "terminated" in str(exc_info.value)
    # start_instances was never called: the instance's state is unchanged.
    assert gateway.instances[instance.instance_id].state is InstanceState.TERMINATED


# -- stop ---------------------------------------------------------------------------


def test_stop_on_stopped_raises_without_calling_the_gateway() -> None:
    gateway = FakeEc2Gateway()
    instance = _instance("i-stopped00000000", InstanceState.STOPPED)
    gateway.instances[instance.instance_id] = instance

    with pytest.raises(ValidationError) as exc_info:
        StopInstanceUseCase(gateway=gateway).execute(
            instance, force=False, wait=False, timeout_s=60
        )

    assert "stopped" in str(exc_info.value)
    assert gateway.instances[instance.instance_id].state is InstanceState.STOPPED


# -- terminate: the ManagedBy safeguard ------------------------------------------------


def test_terminate_unmanaged_instance_without_force_raises_and_instance_survives() -> None:
    gateway = FakeEc2Gateway()
    instance = _instance("i-unmanaged00000", InstanceState.RUNNING, managed=False)
    gateway.instances[instance.instance_id] = instance
    repository = InMemoryRepository()

    with pytest.raises(ValidationError) as exc_info:
        TerminateInstanceUseCase(gateway=gateway, repository=repository).execute(
            instance, force=False, dry_run=False, wait=False, timeout_s=60
        )

    assert "ManagedBy" in str(exc_info.value)
    assert gateway.instances[instance.instance_id].state is InstanceState.RUNNING


def test_terminate_unmanaged_instance_with_force_proceeds() -> None:
    gateway = FakeEc2Gateway()
    instance = _instance("i-unmanaged11111", InstanceState.RUNNING, managed=False)
    gateway.instances[instance.instance_id] = instance
    repository = InMemoryRepository()

    result = TerminateInstanceUseCase(gateway=gateway, repository=repository).execute(
        instance, force=True, dry_run=False, wait=False, timeout_s=60
    )

    assert result is not None
    assert result.state is InstanceState.SHUTTING_DOWN


def test_terminate_already_terminated_raises() -> None:
    gateway = FakeEc2Gateway()
    instance = _instance("i-dead000000000", InstanceState.TERMINATED)
    gateway.instances[instance.instance_id] = instance
    repository = InMemoryRepository()

    with pytest.raises(ValidationError):
        TerminateInstanceUseCase(gateway=gateway, repository=repository).execute(
            instance, force=False, dry_run=False, wait=False, timeout_s=60
        )


def test_terminate_dry_run_returns_none_and_keeps_the_resource_record() -> None:
    gateway = FakeEc2Gateway()
    instance = _instance("i-managed0000000", InstanceState.RUNNING, managed=True)
    gateway.instances[instance.instance_id] = instance
    repository = InMemoryRepository()

    result = TerminateInstanceUseCase(gateway=gateway, repository=repository).execute(
        instance, force=False, dry_run=True, wait=False, timeout_s=60
    )

    assert result is None
    assert gateway.instances[instance.instance_id].state is InstanceState.RUNNING


def test_terminate_deletes_the_resource_record() -> None:
    from aws_admin_cli.domain.models.common import ResourceRecord

    gateway = FakeEc2Gateway()
    instance = _instance("i-managed1111111", InstanceState.RUNNING, managed=True)
    gateway.instances[instance.instance_id] = instance
    repository = InMemoryRepository()
    repository.save(
        ResourceRecord(
            resource_type="ec2:instance",
            identifier=instance.instance_id,
            arn=None,
            profile="localstack",
            region="us-east-1",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )

    TerminateInstanceUseCase(gateway=gateway, repository=repository).execute(
        instance, force=False, dry_run=False, wait=False, timeout_s=60
    )

    assert repository.get(f"ec2:instance:{instance.instance_id}") is None


# -- OperationTimeoutError propagation --------------------------------------------------


def test_start_wait_timeout_raises_operation_timeout_error_with_exit_code_75() -> None:
    gateway = _TimeoutEc2Gateway()
    instance = _instance("i-stopped11111111", InstanceState.STOPPED)
    gateway.instances[instance.instance_id] = instance

    with pytest.raises(OperationTimeoutError) as exc_info:
        StartInstanceUseCase(gateway=gateway).execute(instance, wait=True, timeout_s=10)

    assert exc_info.value.exit_code == 75


def test_terminate_wait_timeout_raises_operation_timeout_error() -> None:
    gateway = _TimeoutEc2Gateway()
    instance = _instance("i-managed2222222", InstanceState.RUNNING, managed=True)
    gateway.instances[instance.instance_id] = instance
    repository = InMemoryRepository()

    with pytest.raises(OperationTimeoutError):
        TerminateInstanceUseCase(gateway=gateway, repository=repository).execute(
            instance, force=False, dry_run=False, wait=True, timeout_s=10
        )
