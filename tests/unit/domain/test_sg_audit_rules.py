"""Tests for the SG audit rule engine: one class per rule, positive and negative cases."""

import pytest
from aws_admin_cli.domain.models.findings import Severity
from aws_admin_cli.domain.models.vpc import SecurityGroup
from aws_admin_cli.domain.policies.sg_audit_rules import (
    _DATABASE_PORTS,
    _RULES,
    audit_security_group,
    audit_security_groups,
    rule_all_protocols_open,
    rule_database_port_open,
    rule_no_ingress_rules,
    rule_other_port_open,
    rule_rdp_open,
    rule_ssh_open,
    rule_unrestricted_egress,
    rule_wide_port_range_open,
)


def _sg(
    *,
    group_id: str = "sg-test",
    group_name: str = "test-sg",
    ingress: list[dict[str, object]] | None = None,
    egress: list[dict[str, object]] | None = None,
) -> SecurityGroup:
    return SecurityGroup.model_validate(
        {
            "GroupId": group_id,
            "GroupName": group_name,
            "VpcId": "vpc-test",
            "Description": "test",
            "IpPermissions": ingress or [],
            "IpPermissionsEgress": egress or [],
        }
    )


def _world_rule(protocol: str, from_port: int | None, to_port: int | None) -> dict[str, object]:
    rule: dict[str, object] = {"IpProtocol": protocol, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
    if from_port is not None:
        rule["FromPort"] = from_port
    if to_port is not None:
        rule["ToPort"] = to_port
    return rule


def _private_rule(protocol: str, from_port: int | None, to_port: int | None) -> dict[str, object]:
    rule: dict[str, object] = {"IpProtocol": protocol, "IpRanges": [{"CidrIp": "10.0.0.0/16"}]}
    if from_port is not None:
        rule["FromPort"] = from_port
    if to_port is not None:
        rule["ToPort"] = to_port
    return rule


# -- SG001: SSH open to the world -------------------------------------------------


class TestSg001SshOpen:
    def test_positive_ssh_open_to_world(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", 22, 22)])
        findings = rule_ssh_open(sg)
        assert len(findings) == 1
        assert findings[0].rule_id == "SG001"
        assert findings[0].severity == Severity.CRITICAL

    def test_negative_ssh_restricted_to_private_cidr(self) -> None:
        sg = _sg(ingress=[_private_rule("tcp", 22, 22)])
        assert rule_ssh_open(sg) == []

    def test_negative_non_tcp_udp_protocol_does_not_match_port(self) -> None:
        # icmp has no ports -- _permission_includes_port must not treat it as
        # covering 22 just because it's open to the world.
        sg = _sg(ingress=[_world_rule("icmp", None, None)])
        assert rule_ssh_open(sg) == []

    def test_negative_tcp_rule_with_no_port_range_does_not_match(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", None, None)])
        assert rule_ssh_open(sg) == []

    def test_negative_ipv6_world_also_flags(self) -> None:
        sg = _sg(
            ingress=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "Ipv6Ranges": [{"CidrIpv6": "::/0"}],
                }
            ]
        )
        assert len(rule_ssh_open(sg)) == 1


# -- SG002: RDP open to the world -------------------------------------------------


class TestSg002RdpOpen:
    def test_positive_rdp_open_to_world(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", 3389, 3389)])
        findings = rule_rdp_open(sg)
        assert len(findings) == 1
        assert findings[0].rule_id == "SG002"
        assert findings[0].severity == Severity.CRITICAL

    def test_negative_rdp_restricted(self) -> None:
        sg = _sg(ingress=[_private_rule("tcp", 3389, 3389)])
        assert rule_rdp_open(sg) == []


# -- SG003: all protocols open to the world ---------------------------------------


class TestSg003AllProtocolsOpen:
    def test_positive_all_protocols_open_to_world(self) -> None:
        sg = _sg(ingress=[_world_rule("-1", None, None)])
        findings = rule_all_protocols_open(sg)
        assert len(findings) == 1
        assert findings[0].rule_id == "SG003"
        assert findings[0].severity == Severity.CRITICAL

    def test_negative_all_protocols_restricted_to_private_cidr(self) -> None:
        sg = _sg(ingress=[_private_rule("-1", None, None)])
        assert rule_all_protocols_open(sg) == []


# -- SG004: database port open to the world ---------------------------------------


class TestSg004DatabasePortOpen:
    def test_positive_postgres_open_to_world(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", 5432, 5432)])
        findings = rule_database_port_open(sg)
        assert len(findings) == 1
        assert findings[0].rule_id == "SG004"
        assert "PostgreSQL" in findings[0].title

    def test_negative_database_port_restricted(self) -> None:
        sg = _sg(ingress=[_private_rule("tcp", 5432, 5432)])
        assert rule_database_port_open(sg) == []

    @pytest.mark.parametrize(("port", "engine"), sorted(_DATABASE_PORTS.items()))
    def test_all_ten_database_ports(self, port: int, engine: str) -> None:
        sg = _sg(ingress=[_world_rule("tcp", port, port)])
        findings = rule_database_port_open(sg)
        assert len(findings) == 1
        assert engine in findings[0].title
        assert str(port) in findings[0].detail


# -- SG005: wide port range open to the world -------------------------------------


class TestSg005WidePortRangeOpen:
    def test_positive_wide_range_open_to_world(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", 1024, 65535)])
        findings = rule_wide_port_range_open(sg)
        assert len(findings) == 1
        assert findings[0].rule_id == "SG005"
        assert findings[0].severity == Severity.HIGH

    def test_negative_narrow_range_does_not_trigger(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", 8080, 8090)])  # 11 ports, under the threshold
        assert rule_wide_port_range_open(sg) == []

    def test_negative_wide_range_restricted_to_private_cidr(self) -> None:
        sg = _sg(ingress=[_private_rule("tcp", 1024, 65535)])
        assert rule_wide_port_range_open(sg) == []

    def test_negative_all_protocols_does_not_also_trigger_sg005(self) -> None:
        sg = _sg(ingress=[_world_rule("-1", None, None)])
        assert rule_wide_port_range_open(sg) == []


# -- SG006: other port open to the world -------------------------------------------


class TestSg006OtherPortOpen:
    def test_positive_uncommon_port_open_to_world(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", 8080, 8080)])
        findings = rule_other_port_open(sg)
        assert len(findings) == 1
        assert findings[0].rule_id == "SG006"
        assert findings[0].severity == Severity.MEDIUM

    def test_negative_port_443_does_not_trigger(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", 443, 443)])
        assert rule_other_port_open(sg) == []

    def test_negative_ssh_does_not_also_trigger_sg006(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", 22, 22)])
        assert rule_other_port_open(sg) == []

    def test_negative_port_restricted_to_private_cidr(self) -> None:
        sg = _sg(ingress=[_private_rule("tcp", 8080, 8080)])
        assert rule_other_port_open(sg) == []

    def test_negative_all_protocols_does_not_also_trigger_sg006(self) -> None:
        sg = _sg(ingress=[_world_rule("-1", None, None)])
        assert rule_other_port_open(sg) == []

    def test_negative_wide_range_does_not_also_trigger_sg006(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", 1024, 65535)])  # already SG005
        assert rule_other_port_open(sg) == []


# -- SG007: unrestricted egress (informational) ------------------------------------


class TestSg007UnrestrictedEgress:
    def test_positive_default_egress_is_flagged(self) -> None:
        sg = _sg(egress=[_world_rule("-1", None, None)])
        findings = rule_unrestricted_egress(sg)
        assert len(findings) == 1
        assert findings[0].rule_id == "SG007"
        assert findings[0].severity == Severity.LOW

    def test_negative_restricted_egress_not_flagged(self) -> None:
        sg = _sg(egress=[_private_rule("-1", None, None)])
        assert rule_unrestricted_egress(sg) == []


# -- SG008: no ingress rules at all -------------------------------------------------


class TestSg008NoIngressRules:
    def test_positive_no_ingress_rules(self) -> None:
        sg = _sg(ingress=[])
        findings = rule_no_ingress_rules(sg)
        assert len(findings) == 1
        assert findings[0].rule_id == "SG008"
        assert findings[0].severity == Severity.INFO

    def test_negative_has_ingress_rules(self) -> None:
        sg = _sg(ingress=[_world_rule("tcp", 443, 443)])
        assert rule_no_ingress_rules(sg) == []


# -- Registry / engine-level guarantees ---------------------------------------------


def test_every_rule_id_in_the_registry_is_unique() -> None:
    # One SG per rule, engineered to trigger exactly that rule's positive case --
    # _RULES is declared in SG001..SG008 order, so zip() pairs each rule function
    # with its own designed trigger.
    positive_cases = [
        _sg(ingress=[_world_rule("tcp", 22, 22)]),  # SG001
        _sg(ingress=[_world_rule("tcp", 3389, 3389)]),  # SG002
        _sg(ingress=[_world_rule("-1", None, None)]),  # SG003
        _sg(ingress=[_world_rule("tcp", 5432, 5432)]),  # SG004
        _sg(ingress=[_world_rule("tcp", 1024, 65535)]),  # SG005
        _sg(ingress=[_world_rule("tcp", 8080, 8080)]),  # SG006
        _sg(egress=[_world_rule("-1", None, None)]),  # SG007
        _sg(ingress=[]),  # SG008
    ]
    assert len(_RULES) == len(positive_cases) == 8

    rule_ids: set[str] = set()
    for rule, sg in zip(_RULES, positive_cases, strict=True):
        findings = rule(sg)
        assert findings, f"{rule.__name__} did not fire for its own designed positive case"
        rule_ids.add(findings[0].rule_id)

    assert len(rule_ids) == 8


def test_clean_security_group_produces_no_findings_at_or_above_medium() -> None:
    sg = _sg(ingress=[_world_rule("tcp", 443, 443)], egress=[_world_rule("-1", None, None)])
    findings = audit_security_group(sg)
    assert all(f.severity.rank < Severity.MEDIUM.rank for f in findings)


def test_bootstrap_bastion_sg_produces_exactly_sg001_critical() -> None:
    sg = _sg(
        group_id="sg-bastion",
        group_name="corp-bastion-sg",
        ingress=[_world_rule("tcp", 22, 22)],
        egress=[_world_rule("-1", None, None)],
    )
    findings = audit_security_group(sg)
    critical = [f for f in findings if f.severity == Severity.CRITICAL]
    assert len(critical) == 1
    assert critical[0].rule_id == "SG001"


def test_audit_security_groups_concatenates_every_groups_findings() -> None:
    clean = _sg(group_id="sg-clean", ingress=[_world_rule("tcp", 443, 443)])
    bastion = _sg(group_id="sg-bastion", ingress=[_world_rule("tcp", 22, 22)])
    findings = audit_security_groups([clean, bastion])
    assert {f.resource_id for f in findings} == {"sg-bastion"}
