"""Use case: reboot a running EC2 instance, validating the state transition first."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.ec2 import Instance
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class RebootInstanceUseCase:
    """Reboot an instance.

    Validates the transition BEFORE calling AWS -- zero gateway calls if the
    instance can't legally reboot from its current state.
    """

    gateway: Ec2Gateway

    def execute(self: Self, instance: Instance) -> None:
        """Reboot ``instance``.

        Raises:
            ValidationError: ``instance`` isn't ``RUNNING``.
        """
        if not instance.state.can_reboot:
            raise ValidationError(instance.state.transition_error("reiniciar"))
        self.gateway.reboot_instances([instance.instance_id], region=instance.region)
