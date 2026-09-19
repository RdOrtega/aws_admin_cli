"""In-memory ``VpcGateway`` test double, pre-loaded with the bootstrap's topology.

Used instead of ``Mock()`` on purpose -- see ``tests/fakes/iam.py`` for why.
Mirrors ``localstack/init/01-bootstrap.sh``'s network exactly (same VPC, same
four subnets, same five security groups with the same deliberately-planted
findings) so unit tests exercise the same shape the e2e tests see against
real LocalStack.

Counts calls to each ``describe_*`` method (``call_counts``) so tests can
verify ``NetworkResolver``'s caching -- resolving several subnets should cost
exactly one ``describe_subnets`` call, not one per subnet.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from aws_admin_cli.domain.models.vpc import (
    AvailabilityZone,
    RouteTable,
    SecurityGroup,
    Subnet,
    Vpc,
)

VPC_ID = "vpc-0a1b2c3d"
IGW_ID = "igw-0a1b2c3d"
PUBLIC_RTB_ID = "rtb-0a1b2c3d"
MAIN_RTB_ID = "rtb-0a1b2c3e"

SUBNET_PUBLIC_1A_ID = "subnet-0a1b2c01"
SUBNET_PUBLIC_1B_ID = "subnet-0a1b2c02"
SUBNET_PRIVATE_1A_ID = "subnet-0a1b2c03"
SUBNET_PRIVATE_1B_ID = "subnet-0a1b2c04"

SG_WEB_ID = "sg-0a1b2c01"
SG_BASTION_ID = "sg-0a1b2c02"
SG_DB_ID = "sg-0a1b2c03"
SG_INTERNAL_ID = "sg-0a1b2c04"
SG_LEGACY_ID = "sg-0a1b2c05"


def _tag(name: str) -> list[dict[str, str]]:
    return [{"Key": "Name", "Value": name}]


def _default_vpcs() -> list[Vpc]:
    return [
        Vpc(
            vpc_id=VPC_ID,
            cidr_block="10.0.0.0/16",
            state="available",
            is_default=False,
            tags=_tag("corp-main-vpc"),
        )
    ]


def _default_subnets() -> list[Subnet]:
    return [
        Subnet(
            subnet_id=SUBNET_PUBLIC_1A_ID,
            vpc_id=VPC_ID,
            cidr_block="10.0.1.0/24",
            availability_zone="us-east-1a",
            available_ip_address_count=251,
            map_public_ip_on_launch=True,
            state="available",
            tags=_tag("corp-public-1a"),
            is_public=True,
        ),
        Subnet(
            subnet_id=SUBNET_PUBLIC_1B_ID,
            vpc_id=VPC_ID,
            cidr_block="10.0.2.0/24",
            availability_zone="us-east-1b",
            available_ip_address_count=251,
            map_public_ip_on_launch=True,
            state="available",
            tags=_tag("corp-public-1b"),
            is_public=True,
        ),
        Subnet(
            subnet_id=SUBNET_PRIVATE_1A_ID,
            vpc_id=VPC_ID,
            cidr_block="10.0.11.0/24",
            availability_zone="us-east-1a",
            available_ip_address_count=251,
            map_public_ip_on_launch=False,
            state="available",
            tags=_tag("corp-private-1a"),
            is_public=False,
        ),
        Subnet(
            subnet_id=SUBNET_PRIVATE_1B_ID,
            vpc_id=VPC_ID,
            cidr_block="10.0.12.0/24",
            availability_zone="us-east-1b",
            available_ip_address_count=251,
            map_public_ip_on_launch=False,
            state="available",
            tags=_tag("corp-private-1b"),
            is_public=False,
        ),
    ]


def _default_security_groups() -> list[SecurityGroup]:
    # Untagged (no Name tag) -- matches the real bootstrap script, which sets
    # GroupName via --group-name and never tags these. Resolution therefore
    # exercises the GroupName-match path, not the tag-Name fallback.
    web = SecurityGroup.model_validate(
        {
            "GroupId": SG_WEB_ID,
            "GroupName": "corp-web-sg",
            "VpcId": VPC_ID,
            "Description": "Trafico web publico (80/443) -- correcto, no es hallazgo",
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 80,
                    "ToPort": 80,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
            ],
            "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        }
    )
    bastion = SecurityGroup.model_validate(
        {
            "GroupId": SG_BASTION_ID,
            "GroupName": "corp-bastion-sg",
            "VpcId": VPC_ID,
            "Description": "HALLAZGO CRITICO: SSH abierto al mundo",
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
            "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        }
    )
    db = SecurityGroup.model_validate(
        {
            "GroupId": SG_DB_ID,
            "GroupName": "corp-db-sg",
            "VpcId": VPC_ID,
            "Description": "HALLAZGO CRITICO: PostgreSQL abierto al mundo",
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 5432,
                    "ToPort": 5432,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
            "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        }
    )
    internal = SecurityGroup.model_validate(
        {
            "GroupId": SG_INTERNAL_ID,
            "GroupName": "corp-internal-sg",
            "VpcId": VPC_ID,
            "Description": "Correcto: todos los protocolos, pero solo origen privado",
            "IpPermissions": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "10.0.0.0/16"}]}],
            "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        }
    )
    legacy = SecurityGroup.model_validate(
        {
            "GroupId": SG_LEGACY_ID,
            "GroupName": "corp-legacy-sg",
            "VpcId": VPC_ID,
            "Description": "HALLAZGO ALTO: rango amplio de puertos abierto al mundo",
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 1024,
                    "ToPort": 65535,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
            "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        }
    )
    return [web, bastion, db, internal, legacy]


def _default_route_tables() -> list[RouteTable]:
    return [
        RouteTable(
            route_table_id=PUBLIC_RTB_ID,
            vpc_id=VPC_ID,
            routes=[
                {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"},
                {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": IGW_ID},
            ],
            associations=[
                {"SubnetId": SUBNET_PUBLIC_1A_ID, "Main": False},
                {"SubnetId": SUBNET_PUBLIC_1B_ID, "Main": False},
            ],
        ),
        RouteTable(
            route_table_id=MAIN_RTB_ID,
            vpc_id=VPC_ID,
            routes=[{"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"}],
            associations=[{"Main": True}],
        ),
    ]


def _default_azs() -> list[AvailabilityZone]:
    return [
        AvailabilityZone(zone_name="us-east-1a", zone_id="use1-az1", state="available"),
        AvailabilityZone(zone_name="us-east-1b", zone_id="use1-az2", state="available"),
    ]


@dataclass
class FakeVpcGateway:
    """A structurally-typed ``VpcGateway`` double, pre-loaded with the bootstrap's network."""

    vpcs: list[Vpc] = field(default_factory=_default_vpcs)
    subnets: list[Subnet] = field(default_factory=_default_subnets)
    security_groups: list[SecurityGroup] = field(default_factory=_default_security_groups)
    route_tables: list[RouteTable] = field(default_factory=_default_route_tables)
    availability_zones: list[AvailabilityZone] = field(default_factory=_default_azs)
    call_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def describe_vpcs(
        self,
        vpc_ids: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Vpc]:
        del region
        self.call_counts["describe_vpcs"] += 1
        result = self.vpcs
        if vpc_ids:
            result = [v for v in result if v.vpc_id in vpc_ids]
        return list(result)

    def describe_subnets(
        self,
        subnet_ids: Sequence[str] | None,
        vpc_id: str | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Subnet]:
        del region
        self.call_counts["describe_subnets"] += 1
        result = self.subnets
        if subnet_ids:
            result = [s for s in result if s.subnet_id in subnet_ids]
        if vpc_id:
            result = [s for s in result if s.vpc_id == vpc_id]
        return list(result)

    def describe_security_groups(
        self,
        group_ids: Sequence[str] | None,
        vpc_id: str | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[SecurityGroup]:
        del region
        self.call_counts["describe_security_groups"] += 1
        result = self.security_groups
        if group_ids:
            result = [g for g in result if g.group_id in group_ids]
        if vpc_id:
            result = [g for g in result if g.vpc_id == vpc_id]
        return list(result)

    def describe_route_tables(
        self, vpc_id: str | None, *, region: str | None = None
    ) -> list[RouteTable]:
        del region
        self.call_counts["describe_route_tables"] += 1
        result = self.route_tables
        if vpc_id:
            result = [rt for rt in result if rt.vpc_id == vpc_id]
        return list(result)

    def describe_availability_zones(self) -> list[AvailabilityZone]:
        self.call_counts["describe_availability_zones"] += 1
        return list(self.availability_zones)
