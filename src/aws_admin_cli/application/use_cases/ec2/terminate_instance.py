"""Use case: terminate an EC2 instance -- the safeguard-heaviest use case in this module."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.ec2 import Instance, InstanceState
from aws_admin_cli.domain.policies.launch_rules import check_managed_tag
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway
from aws_admin_cli.domain.ports.repository import Repository

_DEFAULT_POLL_SECONDS = 5


@dataclass(frozen=True, slots=True)
class TerminateInstanceUseCase:
    """Terminate an instance -- refuses an instance this CLI didn't create, unless ``force``.

    Two checks gate this, in order: the state machine (an already-terminated
    or terminating instance can't be terminated again), then
    ``check_managed_tag`` -- this CLI never destroys what it didn't create,
    without an explicit override.
    """

    gateway: Ec2Gateway
    repository: Repository[ResourceRecord]

    def execute(
        self: Self, instance: Instance, *, force: bool, dry_run: bool, wait: bool, timeout_s: int
    ) -> Instance | None:
        """Terminate ``instance``.

        Returns:
            The instance's post-termination state -- or ``None`` for a
            successful ``dry_run`` (nothing was actually terminated, so
            there's nothing new to report).

        Raises:
            ValidationError: ``instance`` can't be terminated from its
                current state, or lacks the ``ManagedBy=aws-admin-cli`` tag
                and ``force`` is ``False``.
        """
        if not instance.state.can_terminate:
            raise ValidationError(instance.state.transition_error("terminar"))
        check_managed_tag(instance, force=force)

        region = instance.region
        self.gateway.terminate_instances([instance.instance_id], dry_run=dry_run, region=region)
        if dry_run:
            return None

        if wait:
            self.gateway.wait_for_state(
                [instance.instance_id],
                InstanceState.TERMINATED,
                timeout_s=timeout_s,
                poll_s=_DEFAULT_POLL_SECONDS,
                region=region,
            )

        self.repository.delete(f"ec2:instance:{instance.instance_id}")
        return self.gateway.get_instance(instance.instance_id, region=region)
