"""Use case: fetch when an access key was last used."""

from dataclasses import dataclass
from datetime import datetime
from typing import Self

from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class GetAccessKeyLastUsedUseCase:
    """Fetch a single access key's last-used timestamp (``None`` if never used)."""

    gateway: IamGateway

    def execute(self: Self, access_key_id: str) -> datetime | None:
        """Return when ``access_key_id`` was last used."""
        return self.gateway.get_access_key_last_used(access_key_id)
