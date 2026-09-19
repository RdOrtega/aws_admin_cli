"""The VPC TUI screen: read-only network inspection and the security-group audit.

Strictly query-only, mirroring ``presentation/cli/vpc_app.py``'s own
guarantee (see its module docstring: "NO command in this module is
destructive") -- this flow never offers a create/modify/delete option for
any network resource. The disabled "Network changes" entry below exists so a
user looking for one finds an explanation instead of a dead end:
network changes are Networking's job, not this tool's.

Not currently reachable from the TUI's main menu (which was simplified to
S3/IAM/EC2 only) -- it stays fully implemented and read-only-guaranteed, and
remains reachable through the non-interactive CLI (``aws-admin-cli vpc ...``).
"""

from dataclasses import dataclass
from typing import ClassVar, Self

from aws_admin_cli.application.dto.vpc import AuditSecurityGroupsRequest, ListSubnetsRequest
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.tui.menu import (
    NAV_BACK,
    NAV_EXIT,
    Choice,
    Separator,
    back_and_exit_choices,
)
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter
from aws_admin_cli.presentation.wiring import build_vpc_use_cases

__all__ = ["VpcFlow"]

_LIST_VPCS = "list_vpcs"
_LIST_SUBNETS = "list_subnets"
_LIST_SGS = "list_sgs"
_AUDIT_SGS = "audit_sgs"
_LIST_AZS = "list_azs"
_NETWORK_CHANGES = "network_changes"


@dataclass(slots=True)
class VpcFlow:
    """VPC's top-level TUI screen: read-only inspection, never a mutation."""

    title: ClassVar[str] = "VPC (read-only)"

    ctx: AppContext
    prompter: Prompter

    def menu(self: Self) -> NavAction:
        """Show VPC's menu once."""
        selected = self.prompter.select("VPC -- what do you want to query?", self._choices())
        if selected is None or selected == NAV_EXIT:
            return NavAction.EXIT
        if selected == NAV_BACK:
            return NavAction.BACK
        if selected == _NETWORK_CHANGES:
            self.ctx.err_console.print(
                "[yellow]Network changes (creating/modifying VPCs, subnets, security "
                "groups, routes...) are the Networking team's responsibility, not this "
                "tool's -- this module is intentionally read-only.[/]"
            )
            self.prompter.pause()
            return NavAction.STAY
        {
            _LIST_VPCS: self._list_vpcs,
            _LIST_SUBNETS: self._list_subnets,
            _LIST_SGS: self._list_security_groups,
            _AUDIT_SGS: self._audit_security_groups,
            _LIST_AZS: self._list_availability_zones,
        }[selected]()
        return NavAction.STAY

    def _choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(title="List VPCs", value=_LIST_VPCS),
            Choice(title="List subnets", value=_LIST_SUBNETS),
            Choice(title="List security groups", value=_LIST_SGS),
            Choice(title="Audit security groups", value=_AUDIT_SGS),
            Choice(title="List availability zones", value=_LIST_AZS),
            Separator(),
            Choice(
                title="Network changes (create/modify)",
                value=_NETWORK_CHANGES,
                disabled="Read-only -- managed by Networking",
            ),
            *back_and_exit_choices(),
        ]

    def _list_vpcs(self: Self) -> None:
        vpcs = build_vpc_use_cases(self.ctx).list_vpcs.execute()
        if not vpcs:
            self.ctx.err_console.print("[yellow]No VPCs found.[/]")
            self.prompter.pause()
            return
        render(
            [
                {"VpcId": v.vpc_id, "CidrBlock": v.cidr_block, "IsDefault": v.is_default}
                for v in vpcs
            ],
            ctx=self.ctx,
            title="vpc list",
        )
        self.prompter.pause()

    def _list_subnets(self: Self) -> None:
        subnets = build_vpc_use_cases(self.ctx).list_subnets.execute(ListSubnetsRequest())
        if not subnets:
            self.ctx.err_console.print("[yellow]No subnets found.[/]")
            self.prompter.pause()
            return
        render(
            [
                {"SubnetId": s.subnet_id, "VpcId": s.vpc_id, "CidrBlock": s.cidr_block}
                for s in subnets
            ],
            ctx=self.ctx,
            title="subnet list",
        )
        self.prompter.pause()

    def _list_security_groups(self: Self) -> None:
        groups = build_vpc_use_cases(self.ctx).list_security_groups.execute(None)
        if not groups:
            self.ctx.err_console.print("[yellow]No security groups found.[/]")
            self.prompter.pause()
            return
        render(
            [{"GroupId": g.group_id, "GroupName": g.group_name, "VpcId": g.vpc_id} for g in groups],
            ctx=self.ctx,
            title="security group list",
        )
        self.prompter.pause()

    def _audit_security_groups(self: Self) -> None:
        findings = build_vpc_use_cases(self.ctx).audit_security_groups.execute(
            AuditSecurityGroupsRequest()
        )
        if not findings:
            self.ctx.err_console.print("[green]No findings.[/]")
            self.prompter.pause()
            return
        render(
            [
                {
                    "ResourceId": f.resource_id,
                    "Severity": f.severity.value,
                    "RuleId": f.rule_id,
                    "Title": f.title,
                }
                for f in findings
            ],
            ctx=self.ctx,
            title="security group audit",
        )
        self.prompter.pause()

    def _list_availability_zones(self: Self) -> None:
        azs = build_vpc_use_cases(self.ctx).list_availability_zones.execute()
        if not azs:
            self.ctx.err_console.print("[yellow]No availability zones found.[/]")
            self.prompter.pause()
            return
        render(
            [{"ZoneName": az.zone_name, "State": az.state} for az in azs],
            ctx=self.ctx,
            title="availability zone list",
        )
        self.prompter.pause()
