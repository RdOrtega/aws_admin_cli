"""Tests for AuditSecurityGroupsUseCase: filtering by min_severity and ordering."""

from aws_admin_cli.application.dto.vpc import AuditSecurityGroupsRequest
from aws_admin_cli.application.use_cases.vpc.audit_security_groups import (
    AuditSecurityGroupsUseCase,
)
from aws_admin_cli.domain.models.findings import Severity
from aws_admin_cli.domain.models.vpc import SecurityGroup

from tests.fakes.vpc import FakeVpcGateway


def _sg(group_id: str, ingress: list[dict[str, object]]) -> SecurityGroup:
    return SecurityGroup.model_validate(
        {
            "GroupId": group_id,
            "GroupName": group_id,
            "VpcId": "vpc-test",
            "Description": "test",
            "IpPermissions": ingress,
        }
    )


def test_min_severity_high_excludes_medium_low_and_info() -> None:
    critical_sg = _sg(
        "sg-critical",
        [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )
    medium_sg = _sg(
        "sg-medium",
        [
            {
                "IpProtocol": "tcp",
                "FromPort": 8080,
                "ToPort": 8080,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )
    gateway = FakeVpcGateway(security_groups=[critical_sg, medium_sg])
    use_case = AuditSecurityGroupsUseCase(gateway=gateway)

    findings = use_case.execute(AuditSecurityGroupsRequest(min_severity=Severity.HIGH))

    assert all(f.severity.rank >= Severity.HIGH.rank for f in findings)
    assert all(f.resource_id != "sg-medium" for f in findings)
    assert any(f.resource_id == "sg-critical" for f in findings)


def test_findings_are_ordered_critical_first() -> None:
    critical_sg = _sg(
        "sg-a-critical",
        [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )
    high_sg = _sg(
        "sg-b-high",
        [
            {
                "IpProtocol": "tcp",
                "FromPort": 1024,
                "ToPort": 65535,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )
    gateway = FakeVpcGateway(security_groups=[high_sg, critical_sg])
    use_case = AuditSecurityGroupsUseCase(gateway=gateway)

    findings = use_case.execute(AuditSecurityGroupsRequest())

    assert findings[0].severity == Severity.CRITICAL
    ranks = [f.severity.rank for f in findings]
    assert ranks == sorted(ranks, reverse=True)


def test_no_findings_returns_empty_list_without_raising() -> None:
    clean_sg = _sg(
        "sg-clean",
        [
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )
    gateway = FakeVpcGateway(security_groups=[clean_sg])
    use_case = AuditSecurityGroupsUseCase(gateway=gateway)

    findings = use_case.execute(AuditSecurityGroupsRequest(min_severity=Severity.CRITICAL))

    assert findings == []
