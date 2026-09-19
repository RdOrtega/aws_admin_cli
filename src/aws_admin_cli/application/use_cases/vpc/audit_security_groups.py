"""Use case: audit security groups against the domain's SG rule engine."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.vpc import AuditSecurityGroupsRequest
from aws_admin_cli.domain.models.findings import SecurityFinding
from aws_admin_cli.domain.policies.sg_audit_rules import audit_security_groups
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway


@dataclass(frozen=True, slots=True)
class AuditSecurityGroupsUseCase:
    """Orchestrate a security-group audit: fetch, delegate to the domain rule engine, filter, sort.

    All the actual rule logic lives in ``domain/policies/sg_audit_rules.py``
    -- this use case only fetches the security groups to audit and applies
    the ``min_severity`` filter and the final ordering; it never evaluates a
    rule itself.
    """

    gateway: VpcGateway

    def execute(self: Self, request: AuditSecurityGroupsRequest) -> list[SecurityFinding]:
        """Return findings for ``request.vpc_id`` (or every VPC), filtered and sorted.

        Returns:
            Findings with ``severity.rank >= request.min_severity.rank``,
            ordered by severity descending, then by ``resource_id``. An empty
            list if nothing matches -- never an error.
        """
        groups = self.gateway.describe_security_groups(None, request.vpc_id, None)
        findings = audit_security_groups(groups)
        filtered = [f for f in findings if f.severity.rank >= request.min_severity.rank]
        filtered.sort(key=lambda f: (-f.severity.rank, f.resource_id))
        return filtered
