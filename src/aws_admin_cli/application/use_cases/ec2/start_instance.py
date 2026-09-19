"""Use case: start a stopped EC2 instance, validating the state transition first."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.ec2 import Instance, InstanceState
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway

_DEFAULT_POLL_SECONDS = 5


@dataclass(frozen=True, slots=True)
class StartInstanceUseCase:
    """Start an instance.

    Validates the transition BEFORE calling AWS -- zero gateway calls if the
    instance can't legally start from its current state.
    """

    gateway: Ec2Gateway

    def execute(self: Self, instance: Instance, *, wait: bool, timeout_s: int) -> Instance:
        """Start ``instance``.

        Raises:
            ValidationError: ``instance`` isn't ``STOPPED``.
        """
        if not instance.state.can_start:
            raise ValidationError(instance.state.transition_error("iniciar"))

        region = instance.region
        self.gateway.start_instances([instance.instance_id], region=region)
        if wait:
            self.gateway.wait_for_state(
                [instance.instance_id],
                InstanceState.RUNNING,
                timeout_s=timeout_s,
                poll_s=_DEFAULT_POLL_SECONDS,
                region=region,
            )
        return self.gateway.get_instance(instance.instance_id, region=region)
