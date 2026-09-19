"""Use case: unlink a user's MFA device, optionally deleting it outright."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import DeactivateMfaDeviceRequest
from aws_admin_cli.domain.ports.iam_gateway import IamGateway

_VIRTUAL_MFA_ARN_MARKER = ":mfa/"


@dataclass(frozen=True, slots=True)
class DeactivateMfaDeviceUseCase:
    """Deactivate a user's MFA device, deleting the device object too if virtual and asked to."""

    gateway: IamGateway

    def execute(self: Self, request: DeactivateMfaDeviceRequest) -> None:
        """Deactivate ``request.serial_number``, then delete it if it's virtual and requested.

        A hardware device's serial number never contains ``:mfa/`` (that shape
        is unique to a virtual device's ARN), so the delete call is simply
        skipped for one -- there's no separate "is this virtual?" check to get
        wrong.
        """
        self.gateway.deactivate_mfa_device(request.user_name, request.serial_number)
        if request.delete_virtual_device and _VIRTUAL_MFA_ARN_MARKER in request.serial_number:
            self.gateway.delete_virtual_mfa_device(request.serial_number)
