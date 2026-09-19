"""Use case: stop a running EC2 instance, validating the state transition first."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.ec2 import Instance, InstanceState
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway

_DEFAULT_POLL_SECONDS = 5


@dataclass(frozen=True, slots=True)
class StopInstanceUseCase:
    """Stop an instance.

    Validates the transition BEFORE calling AWS -- zero gateway calls if the
    instance can't legally stop from its current state.
    """

    gateway: Ec2Gateway

    def execute(
        self: Self, instance: Instance, *, force: bool, wait: bool, timeout_s: int
    ) -> Instance:
        """Stop ``instance``.

        Args:
            instance: The already-resolved instance to stop.
            force: A forced stop is the software equivalent of pulling the
                power cable -- it skips the OS's own graceful shutdown, which
                can corrupt in-flight writes on the instance's filesystem.
                Reserved for an instance that's stuck and not responding to a
                normal stop.
            wait: Whether to block until the instance reaches ``STOPPED``.
            timeout_s: Wait timeout, in seconds.

        Raises:
            ValidationError: ``instance`` isn't ``RUNNING``.
        """
        if not instance.state.can_stop:
            raise ValidationError(instance.state.transition_error("detener"))

        region = instance.region
        self.gateway.stop_instances([instance.instance_id], force=force, region=region)
        if wait:
            self.gateway.wait_for_state(
                [instance.instance_id],
                InstanceState.STOPPED,
                timeout_s=timeout_s,
                poll_s=_DEFAULT_POLL_SECONDS,
                region=region,
            )
        return self.gateway.get_instance(instance.instance_id, region=region)
