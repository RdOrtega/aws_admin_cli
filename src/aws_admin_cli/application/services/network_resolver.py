"""``NetworkResolver``: translate human-friendly network references into resources.

Consumed today by the ``vpc resolve`` diagnostic command, and -- this is the
whole reason it exists as a reusable service instead of being inlined into
that one command -- by Fase 5's EC2 use cases, so ``ec2 run --subnet
corp-private-1a --sg corp-web-sg`` can accept the same names Networking uses
day to day instead of forcing the caller to already know
``subnet-0a1b2c3d4e5f6a7b8``.

Pure application layer: depends only on the ``VpcGateway`` Protocol. No
boto3, no typer, no I/O beyond what the gateway itself does.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Self

from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.vpc import SecurityGroup, Subnet, Vpc
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway

_VPC_ID_RE = re.compile(r"^vpc-[0-9a-f]{8,17}$")
_SUBNET_ID_RE = re.compile(r"^subnet-[0-9a-f]{8,17}$")
_SG_ID_RE = re.compile(r"^sg-[0-9a-f]{8,17}$")

_MAX_CANDIDATES_SHOWN = 10
_ALL_VPCS_KEY = "__all__"


def _format_candidates(candidates: Sequence[tuple[str, str | None]]) -> str:
    """Render up to 10 ``(id, name)`` candidates as ``"id (name), id (name), ..."``.

    An error that says "no existe" without saying what DOES exist forces the
    caller to go run a `list` command just to retry -- every not-found/
    ambiguous error from this resolver includes this instead.
    """
    shown = candidates[:_MAX_CANDIDATES_SHOWN]
    rendered = [f"{cid} ({name})" if name else cid for cid, name in shown]
    remaining = len(candidates) - len(shown)
    suffix = f", y {remaining} más" if remaining > 0 else ""
    return ", ".join(rendered) + suffix


@dataclass(slots=True)
class NetworkResolver:
    """Resolves human references (``vpc-...``, a Name tag, a bare bucket-style name) to resources.

    Caches every ``describe_*`` call it makes (keyed by the scoping VPC, or a
    single "all" bucket when unscoped) for the lifetime of this instance --
    resolving several subnets in a row costs exactly one ``describe_subnets``
    call, not one per subnet.
    """

    gateway: VpcGateway
    _vpcs_cache: dict[str, list[Vpc]] = field(default_factory=dict, init=False, repr=False)
    _subnets_cache: dict[str, list[Subnet]] = field(default_factory=dict, init=False, repr=False)
    _sgs_cache: dict[str, list[SecurityGroup]] = field(default_factory=dict, init=False, repr=False)

    def _all_vpcs(self: Self) -> list[Vpc]:
        cached = self._vpcs_cache.get(_ALL_VPCS_KEY)
        if cached is not None:
            return cached
        vpcs = self.gateway.describe_vpcs(None, None)
        self._vpcs_cache[_ALL_VPCS_KEY] = vpcs
        return vpcs

    def _subnets_in(self: Self, vpc: Vpc | None) -> list[Subnet]:
        cache_key = vpc.vpc_id if vpc is not None else _ALL_VPCS_KEY
        cached = self._subnets_cache.get(cache_key)
        if cached is not None:
            return cached
        subnets = self.gateway.describe_subnets(None, vpc.vpc_id if vpc else None, None)
        self._subnets_cache[cache_key] = subnets
        return subnets

    def _security_groups_in(self: Self, vpc: Vpc | None) -> list[SecurityGroup]:
        cache_key = vpc.vpc_id if vpc is not None else _ALL_VPCS_KEY
        cached = self._sgs_cache.get(cache_key)
        if cached is not None:
            return cached
        groups = self.gateway.describe_security_groups(None, vpc.vpc_id if vpc else None, None)
        self._sgs_cache[cache_key] = groups
        return groups

    def resolve_vpc(self: Self, ref: str | None) -> Vpc:
        """Resolve a VPC reference: an ID, a Name tag, or ``None`` for "the default/only one".

        Args:
            ref: ``"vpc-xxxxxxxx"`` for a direct ID lookup, any other string
                for an exact Name-tag lookup, or ``None`` to pick the
                account's default VPC (falling back to the single VPC that
                exists, if there's exactly one and no default).

        Raises:
            ResourceNotFoundError: No VPC matches ``ref``.
            ValidationError: ``ref`` is ``None``, there's no default VPC, and
                more than one VPC exists (candidates are listed).
        """
        vpcs = self._all_vpcs()

        if ref is None:
            defaults = [vpc for vpc in vpcs if vpc.is_default]
            if defaults:
                return defaults[0]
            if len(vpcs) == 1:
                return vpcs[0]
            raise ValidationError(
                "Hay varias VPCs y ninguna es la VPC por defecto.",
                hint=(
                    "Especifica --vpc. Candidatas: "
                    + _format_candidates([(vpc.vpc_id, vpc.name) for vpc in vpcs])
                ),
            )

        if _VPC_ID_RE.match(ref):
            match = next((vpc for vpc in vpcs if vpc.vpc_id == ref), None)
            if match is not None:
                return match
        else:
            match = next((vpc for vpc in vpcs if vpc.name == ref), None)
            if match is not None:
                return match

        raise ResourceNotFoundError(
            f"No se encontró ninguna VPC que coincida con '{ref}'.",
            hint="VPCs disponibles: " + _format_candidates([(v.vpc_id, v.name) for v in vpcs]),
        )

    def resolve_subnet(self: Self, ref: str, *, vpc: Vpc | None = None) -> Subnet:
        """Resolve a subnet reference: an ID, or an exact Name tag.

        Args:
            ref: ``"subnet-xxxxxxxx"`` for a direct ID lookup, or any other
                string for an exact Name-tag lookup.
            vpc: Optional VPC to scope the search (and the cache) to.

        Raises:
            ResourceNotFoundError: No subnet matches ``ref`` (candidates listed).
            ValidationError: More than one subnet shares the same Name tag
                (all candidates, with their IDs, are listed).
        """
        subnets = self._subnets_in(vpc)
        candidates = [(s.subnet_id, s.name) for s in subnets]

        if _SUBNET_ID_RE.match(ref):
            matches = [s for s in subnets if s.subnet_id == ref]
        else:
            matches = [s for s in subnets if s.name == ref]

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValidationError(
                f"'{ref}' es ambiguo: varias subnets comparten ese nombre.",
                hint=(
                    "Usa el subnet-id para desambiguar. Candidatas: "
                    + _format_candidates([(s.subnet_id, s.name) for s in matches])
                ),
            )
        raise ResourceNotFoundError(
            f"No se encontró ninguna subnet que coincida con '{ref}'.",
            hint="Subnets disponibles: " + _format_candidates(candidates),
        )

    def resolve_subnets(
        self: Self, refs: Sequence[str], *, vpc: Vpc | None = None
    ) -> list[Subnet]:
        """Resolve several subnets in one go (shares the cache with ``resolve_subnet``)."""
        return [self.resolve_subnet(ref, vpc=vpc) for ref in refs]

    def resolve_security_group(self: Self, ref: str, *, vpc: Vpc | None = None) -> SecurityGroup:
        """Resolve a security-group reference: an ID, a GroupName, or a Name tag (in that priority).

        Args:
            ref: ``"sg-xxxxxxxx"`` for a direct ID lookup; otherwise ``ref``
                is first matched against ``GroupName`` (exact), and only if
                that finds nothing is it matched against the Name tag.
            vpc: Optional VPC to scope the search (and the cache) to.

        Raises:
            ResourceNotFoundError: No security group matches ``ref``.
            ValidationError: More than one security group matches at the
                same priority level (all candidates, with their IDs, are listed).
        """
        groups = self._security_groups_in(vpc)
        candidates = [(g.group_id, g.display_name) for g in groups]

        if _SG_ID_RE.match(ref):
            matches = [g for g in groups if g.group_id == ref]
        else:
            matches = [g for g in groups if g.group_name == ref]
            if not matches:
                matches = [g for g in groups if g.name == ref]

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValidationError(
                f"'{ref}' es ambiguo: varios security groups coinciden.",
                hint=(
                    "Usa el group-id para desambiguar. Candidatos: "
                    + _format_candidates([(g.group_id, g.display_name) for g in matches])
                ),
            )
        raise ResourceNotFoundError(
            f"No se encontró ningún security group que coincida con '{ref}'.",
            hint="Security groups disponibles: " + _format_candidates(candidates),
        )

    def resolve_security_groups(
        self: Self, refs: Sequence[str], *, vpc: Vpc | None = None
    ) -> list[SecurityGroup]:
        """Resolve several security-group references in one go (shares the cache)."""
        return [self.resolve_security_group(ref, vpc=vpc) for ref in refs]
