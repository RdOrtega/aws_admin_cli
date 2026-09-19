"""VPC domain models: read-only network resources (VPCs, subnets, security groups).

Same hydration pattern as ``domain/models/iam.py`` and ``domain/models/s3.py``:
PascalCase alias generator matching AWS's own casing, ``populate_by_name=True``
so snake_case kwargs also work (tests), and ``extra="ignore"`` because AWS
routinely adds response fields we don't model.

This module has no write helpers, no ``create_*``/``delete_*`` anything: the
``vpc`` module is read-only by design (see ``domain/ports/vpc_gateway.py``'s
module docstring for why).
"""

from collections.abc import Mapping, Sequence
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_pascal

_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="ignore",
    populate_by_name=True,
    alias_generator=to_pascal,
)

_ALL_PORTS_FROM = 0
_ALL_PORTS_TO = 65535
_WORLD_CIDR_V4 = "0.0.0.0/0"
_WORLD_CIDR_V6 = "::/0"


def tag_value(tags: Sequence[Mapping[str, str]], key: str) -> str | None:
    """Look up a tag's value in AWS's ``[{"Key": ..., "Value": ...}]`` tag list shape.

    EC2 (and every other AWS service) represents tags as a list of
    ``{"Key": ..., "Value": ...}`` pairs, never a plain dict -- this is the one
    place that knows how to read that shape, so every model below (and every
    caller outside this module) can just ask for a tag by name.

    Args:
        tags: The raw tag list, e.g. ``[{"Key": "Name", "Value": "corp-main-vpc"}]``.
        key: The tag key to look up.

    Returns:
        The tag's value, or ``None`` if ``key`` isn't present (including when
        ``tags`` is empty).
    """
    for tag in tags:
        if tag.get("Key") == key:
            return tag.get("Value")
    return None


def tags_to_dict(tags: Sequence[Mapping[str, str]]) -> dict[str, str]:
    """Convert the raw ``[{"Key": ..., "Value": ...}]`` tag list shape into a plain dict.

    The list-to-dict counterpart of :func:`tag_value`, for callers that need
    every tag at once (e.g. rendering a detail view, or inheriting a source
    resource's tags onto a copy) rather than looking up one key.
    """
    return {tag["Key"]: tag["Value"] for tag in tags}


class Vpc(BaseModel):
    """A VPC, as returned by ``DescribeVpcs``."""

    model_config = _MODEL_CONFIG

    vpc_id: str
    cidr_block: str
    state: str
    is_default: bool = False
    tags: list[dict[str, str]] = Field(default_factory=list)

    @property
    def name(self: Self) -> str | None:
        """This VPC's ``Name`` tag, if it has one."""
        return tag_value(self.tags, "Name")

    @property
    def display_name(self: Self) -> str:
        """The ``Name`` tag if set, otherwise the raw ``vpc_id`` -- always non-empty."""
        return self.name or self.vpc_id


class Subnet(BaseModel):
    """A subnet, as returned by ``DescribeSubnets``.

    ``is_public`` is NOT an AWS response field: AWS's own
    ``MapPublicIpOnLaunch`` flag only controls whether an instance launched
    *without* an explicit public-IP choice gets one automatically -- it does
    NOT determine whether the subnet can actually reach the internet. What
    determines that is whether the subnet's associated route table has a
    route to an internet gateway. ``Boto3VpcGateway.describe_subnets`` computes
    this by cross-referencing the VPC's route tables (see its docstring for
    the "main route table" fallback) and fills this field in; a bare
    ``Subnet.model_validate(raw_response)`` with no such cross-reference
    leaves it ``None`` rather than silently guessing.
    """

    model_config = _MODEL_CONFIG

    subnet_id: str
    vpc_id: str
    cidr_block: str
    availability_zone: str
    available_ip_address_count: int
    map_public_ip_on_launch: bool = False
    state: str
    tags: list[dict[str, str]] = Field(default_factory=list)
    is_public: bool | None = Field(default=None, exclude=False)

    @property
    def name(self: Self) -> str | None:
        """This subnet's ``Name`` tag, if it has one."""
        return tag_value(self.tags, "Name")

    @property
    def display_name(self: Self) -> str:
        """The ``Name`` tag if set, otherwise the raw ``subnet_id`` -- always non-empty."""
        return self.name or self.subnet_id


class IpPermission(BaseModel):
    """One ingress or egress rule within a security group.

    AWS's raw shape nests everything (``IpRanges: [{"CidrIp": "..."}]``,
    ``UserIdGroupPairs: [{"GroupId": "..."}]``); the validators below flatten
    those into plain string lists so callers never need to know the nested
    shape.
    """

    model_config = _MODEL_CONFIG

    ip_protocol: str
    from_port: int | None = None
    to_port: int | None = None
    ip_ranges: list[str] = Field(default_factory=list, alias="IpRanges")
    ipv6_ranges: list[str] = Field(default_factory=list, alias="Ipv6Ranges")
    source_group_ids: list[str] = Field(default_factory=list, alias="UserIdGroupPairs")
    prefix_list_ids: list[str] = Field(default_factory=list, alias="PrefixListIds")

    @field_validator("ip_ranges", mode="before")
    @classmethod
    def _flatten_ip_ranges(cls: type[Self], value: object) -> object:
        return _flatten(value, "CidrIp")

    @field_validator("ipv6_ranges", mode="before")
    @classmethod
    def _flatten_ipv6_ranges(cls: type[Self], value: object) -> object:
        return _flatten(value, "CidrIpv6")

    @field_validator("source_group_ids", mode="before")
    @classmethod
    def _flatten_source_groups(cls: type[Self], value: object) -> object:
        return _flatten(value, "GroupId")

    @field_validator("prefix_list_ids", mode="before")
    @classmethod
    def _flatten_prefix_lists(cls: type[Self], value: object) -> object:
        return _flatten(value, "PrefixListId")

    @property
    def is_all_protocols(self: Self) -> bool:
        """Whether this rule covers every IP protocol (AWS's ``"-1"`` wildcard)."""
        return self.ip_protocol == "-1"

    @property
    def is_all_ports(self: Self) -> bool:
        """Whether this rule covers the full 0-65535 port range."""
        if self.is_all_protocols:
            return True
        return self.from_port == _ALL_PORTS_FROM and self.to_port == _ALL_PORTS_TO

    @property
    def port_count(self: Self) -> int:
        """Number of ports covered by this rule (65536 for all-protocols/all-ports)."""
        if self.is_all_protocols or self.from_port is None or self.to_port is None:
            return _ALL_PORTS_TO - _ALL_PORTS_FROM + 1
        return self.to_port - self.from_port + 1

    @property
    def port_range_display(self: Self) -> str:
        """Human-readable port range: ``"22"``, ``"1024-65535"``, or ``"ALL"``."""
        if self.is_all_protocols or self.from_port is None or self.to_port is None:
            return "ALL"
        if self.from_port == self.to_port:
            return str(self.from_port)
        return f"{self.from_port}-{self.to_port}"

    @property
    def open_to_world(self: Self) -> bool:
        """Whether this rule allows traffic from anywhere (IPv4 or IPv6)."""
        return _WORLD_CIDR_V4 in self.ip_ranges or _WORLD_CIDR_V6 in self.ipv6_ranges


def _flatten(value: object, inner_key: str) -> object:
    """Flatten a list of single-key dicts (AWS's nested shape) into a plain value list."""
    if not isinstance(value, list):
        return value
    flattened: list[Any] = []
    for item in value:
        if isinstance(item, Mapping) and inner_key in item:
            flattened.append(item[inner_key])
        else:
            flattened.append(item)
    return flattened


class SecurityGroup(BaseModel):
    """A security group, as returned by ``DescribeSecurityGroups``."""

    model_config = _MODEL_CONFIG

    group_id: str
    group_name: str
    vpc_id: str
    description: str = ""
    ingress: list[IpPermission] = Field(default_factory=list, alias="IpPermissions")
    egress: list[IpPermission] = Field(default_factory=list, alias="IpPermissionsEgress")
    tags: list[dict[str, str]] = Field(default_factory=list)

    @property
    def name(self: Self) -> str | None:
        """This security group's ``Name`` tag, if it has one."""
        return tag_value(self.tags, "Name")

    @property
    def display_name(self: Self) -> str:
        """The ``Name`` tag if set, otherwise the ``GroupName`` -- always non-empty."""
        return self.name or self.group_name


class RouteTable(BaseModel):
    """A route table, as returned by ``DescribeRouteTables``."""

    model_config = _MODEL_CONFIG

    route_table_id: str
    vpc_id: str
    routes: list[dict[str, Any]] = Field(default_factory=list)
    associations: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def has_igw_route(self: Self) -> bool:
        """Whether this table routes ``0.0.0.0/0`` to an internet gateway.

        This -- not ``MapPublicIpOnLaunch`` -- is what actually makes a
        subnet "public" (see ``Subnet.is_public``'s docstring).
        """
        return any(
            route.get("DestinationCidrBlock") == _WORLD_CIDR_V4
            and str(route.get("GatewayId", "")).startswith("igw-")
            for route in self.routes
        )

    @property
    def associated_subnet_ids(self: Self) -> list[str]:
        """Subnet IDs explicitly associated with this route table (excludes the main-table flag)."""
        return [
            assoc["SubnetId"] for assoc in self.associations if assoc.get("SubnetId") is not None
        ]

    @property
    def is_main(self: Self) -> bool:
        """Whether this is the VPC's main (default) route table."""
        return any(assoc.get("Main") for assoc in self.associations)


class AvailabilityZone(BaseModel):
    """An availability zone, as returned by ``DescribeAvailabilityZones``."""

    model_config = _MODEL_CONFIG

    zone_name: str
    zone_id: str
    state: str
