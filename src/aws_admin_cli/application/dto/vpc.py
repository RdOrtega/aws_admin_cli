"""Application-layer DTOs for VPC use cases.

Frozen dataclasses, same convention as ``application/dto/iam.py`` and
``application/dto/s3.py``. There is no ``dto/vpc.py`` entry for anything that
would create or mutate a resource -- this module is read-only, mirroring the
rest of the ``vpc`` module (see ``domain/ports/vpc_gateway.py``).
"""

from dataclasses import dataclass

from aws_admin_cli.domain.models.findings import Severity


@dataclass(frozen=True, slots=True)
class ListSubnetsRequest:
    """Request to list subnets, optionally filtered by VPC, AZ, and public/private."""

    vpc_id: str | None = None
    availability_zone: str | None = None
    public_only: bool = False
    private_only: bool = False


@dataclass(frozen=True, slots=True)
class AuditSecurityGroupsRequest:
    """Request to audit a VPC's (or every VPC's) security groups."""

    vpc_id: str | None = None
    min_severity: Severity = Severity.INFO
