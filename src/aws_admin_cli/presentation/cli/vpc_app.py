"""VPC CLI commands: read-only queries over VPCs, subnets, and security groups.

Same shape as ``iam_app.py``/``s3_app.py``: pull ``AppContext`` off
``ctx.obj``, call ``build_vpc_use_cases(app_ctx)`` (see
``presentation/wiring.py``) to get every VPC use case (and the shared
``NetworkResolver``) already wired, call ``.execute()``/``.resolve_*()``,
hand the result to ``render()``. No business rule lives here.

NO command in this module is destructive -- ``confirm_destructive`` is never
imported, because there is nothing here to confirm: the ``vpc`` module never
creates, modifies, or deletes anything (see
``domain/ports/vpc_gateway.py``'s module docstring, and
``docs/least-privilege.md``).
"""

from typing import Annotated, Any

import typer

from aws_admin_cli.application.dto.vpc import AuditSecurityGroupsRequest, ListSubnetsRequest
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.enums import OutputFormat
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.findings import SecurityFinding, Severity
from aws_admin_cli.domain.models.vpc import IpPermission, SecurityGroup, Subnet, Vpc
from aws_admin_cli.presentation.cli.callbacks import OutputOption, apply_output_override
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.wiring import build_vpc_use_cases

vpc_app = typer.Typer(
    help="Consulta de red (VPC, subnets, security groups). Solo lectura.", no_args_is_help=True
)
subnet_app = typer.Typer(help="Subnets.", no_args_is_help=True)
sg_app = typer.Typer(help="Security groups.", no_args_is_help=True)
az_app = typer.Typer(help="Availability zones.", no_args_is_help=True)
vpc_app.add_typer(subnet_app, name="subnet")
vpc_app.add_typer(sg_app, name="sg")
vpc_app.add_typer(az_app, name="az")

_FINDINGS_EXIT_CODE = 3

VpcOption = Annotated[
    str | None,
    typer.Option("--vpc", help="ID o tag Name de la VPC (por defecto: la única/default)."),
]

_SEVERITY_COLOR: dict[Severity, str] = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "dark_orange",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "blue",
    Severity.INFO: "grey58",
}


def _vpc_row(vpc: Vpc) -> dict[str, Any]:
    return {
        "VpcId": vpc.vpc_id,
        "Name": vpc.name,
        "CidrBlock": vpc.cidr_block,
        "State": vpc.state,
        "IsDefault": vpc.is_default,
    }


def _subnet_row(subnet: Subnet) -> dict[str, Any]:
    visibility = "-" if subnet.is_public is None else ("public" if subnet.is_public else "private")
    return {
        "SubnetId": subnet.subnet_id,
        "Name": subnet.name,
        "VpcId": subnet.vpc_id,
        "CidrBlock": subnet.cidr_block,
        "AvailabilityZone": subnet.availability_zone,
        "AvailableIps": subnet.available_ip_address_count,
        "Visibility": visibility,
        "State": subnet.state,
    }


def _sg_row(sg: SecurityGroup) -> dict[str, Any]:
    return {
        "GroupId": sg.group_id,
        "GroupName": sg.group_name,
        "Name": sg.name,
        "VpcId": sg.vpc_id,
        "IngressRules": len(sg.ingress),
        "EgressRules": len(sg.egress),
    }


def _permission_row(direction: str, permission: IpPermission, *, colored: bool) -> dict[str, Any]:
    sources = (
        ", ".join(
            [
                *permission.ip_ranges,
                *permission.ipv6_ranges,
                *permission.source_group_ids,
                *permission.prefix_list_ids,
            ]
        )
        or "-"
    )
    port_range = permission.port_range_display
    if colored and permission.open_to_world:
        port_range = f"[bold red]{port_range}[/]"
        sources = f"[bold red]{sources}[/]"
    return {
        "Direction": direction,
        "Protocol": "ALL" if permission.is_all_protocols else permission.ip_protocol,
        "PortRange": port_range,
        "Sources": sources,
        "OpenToWorld": permission.open_to_world,
    }


def _finding_row(finding: SecurityFinding, *, colored: bool) -> dict[str, Any]:
    severity = finding.severity.value
    if colored:
        color = _SEVERITY_COLOR[finding.severity]
        severity = f"[{color}]{severity}[/]"
    return {
        "RuleId": finding.rule_id,
        "Severity": severity,
        "ResourceId": finding.resource_id,
        "ResourceName": finding.resource_name,
        "Title": finding.title,
        "Recommendation": finding.recommendation,
    }


def _finding_json(finding: SecurityFinding) -> dict[str, Any]:
    return {
        "RuleId": finding.rule_id,
        "Severity": finding.severity.value,
        "ResourceType": finding.resource_type,
        "ResourceId": finding.resource_id,
        "ResourceName": finding.resource_name,
        "Title": finding.title,
        "Detail": finding.detail,
        "Recommendation": finding.recommendation,
    }


# == VPCs ======================================================================


@vpc_app.command("list")
def vpc_list(ctx: typer.Context, output: OutputOption = None) -> None:
    """Lista las VPCs de esta cuenta."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    use_case = build_vpc_use_cases(app_ctx).list_vpcs
    vpcs = use_case.execute()
    if app_ctx_out.output is OutputFormat.JSON:
        render(
            [v.model_dump(by_alias=True, mode="json") for v in vpcs],
            ctx=app_ctx_out,
            title="vpc list",
        )
        return
    render([_vpc_row(v) for v in vpcs], ctx=app_ctx_out, title="vpc list")


@vpc_app.command("show")
def vpc_show(ctx: typer.Context, ref: str, output: OutputOption = None) -> None:
    """Muestra una VPC: sus subnets, sus security groups, y sus conteos."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    use_cases = build_vpc_use_cases(app_ctx)
    vpc = use_cases.resolver.resolve_vpc(ref)
    details = use_cases.get_vpc_details.execute(vpc)

    if app_ctx_out.output is OutputFormat.JSON:
        payload = {
            "Vpc": details.vpc.model_dump(by_alias=True, mode="json"),
            "Subnets": [s.model_dump(by_alias=True, mode="json") for s in details.subnets],
            "SecurityGroups": [
                sg.model_dump(by_alias=True, mode="json") for sg in details.security_groups
            ],
            "SubnetCount": details.subnet_count,
            "SecurityGroupCount": details.security_group_count,
        }
        render(payload, ctx=app_ctx_out, title="vpc show")
        return

    app_ctx_out.console.print(f"[bold]{details.vpc.display_name}[/] ({details.vpc.vpc_id})")
    app_ctx_out.console.print(
        f"CIDR: {details.vpc.cidr_block}  Estado: {details.vpc.state}  "
        f"Default: {details.vpc.is_default}  "
        f"Subnets: {details.subnet_count}  SGs: {details.security_group_count}"
    )
    render([_subnet_row(s) for s in details.subnets], ctx=app_ctx_out, title="Subnets")
    render(
        [_sg_row(sg) for sg in details.security_groups], ctx=app_ctx_out, title="Security groups"
    )


# == Subnets ===================================================================


@subnet_app.command("list")
def subnet_list(
    ctx: typer.Context,
    vpc: VpcOption = None,
    az: Annotated[str | None, typer.Option("--az", help="Filtra por availability zone.")] = None,
    public: Annotated[bool, typer.Option("--public", help="Solo subnets públicas.")] = False,
    private: Annotated[bool, typer.Option("--private", help="Solo subnets privadas.")] = False,
    output: OutputOption = None,
) -> None:
    """Lista subnets. Muestra las IPs disponibles -- clave para elegir dónde lanzar (Fase 5)."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    if public and private:
        raise ValidationError(
            "--public y --private son mutuamente excluyentes.",
            hint="Usa como mucho una de las dos opciones.",
        )

    use_cases = build_vpc_use_cases(app_ctx)
    vpc_id = use_cases.resolver.resolve_vpc(vpc).vpc_id if vpc else None
    subnets = use_cases.list_subnets.execute(
        ListSubnetsRequest(
            vpc_id=vpc_id, availability_zone=az, public_only=public, private_only=private
        )
    )
    if app_ctx_out.output is OutputFormat.JSON:
        render(
            [s.model_dump(by_alias=True, mode="json") for s in subnets],
            ctx=app_ctx_out,
            title="vpc subnet list",
        )
        return
    render([_subnet_row(s) for s in subnets], ctx=app_ctx_out, title="vpc subnet list")


@subnet_app.command("show")
def subnet_show(
    ctx: typer.Context, ref: str, vpc: VpcOption = None, output: OutputOption = None
) -> None:
    """Muestra una subnet."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    resolver = build_vpc_use_cases(app_ctx).resolver
    scope_vpc = resolver.resolve_vpc(vpc) if vpc else None
    subnet = resolver.resolve_subnet(ref, vpc=scope_vpc)
    if app_ctx_out.output is OutputFormat.JSON:
        render(
            subnet.model_dump(by_alias=True, mode="json"), ctx=app_ctx_out, title="vpc subnet show"
        )
        return
    render(_subnet_row(subnet), ctx=app_ctx_out, title="vpc subnet show")


# == Security groups ===========================================================


@sg_app.command("list")
def sg_list(ctx: typer.Context, vpc: VpcOption = None, output: OutputOption = None) -> None:
    """Lista security groups."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    use_cases = build_vpc_use_cases(app_ctx)
    vpc_id = use_cases.resolver.resolve_vpc(vpc).vpc_id if vpc else None
    groups = use_cases.list_security_groups.execute(vpc_id)
    if app_ctx_out.output is OutputFormat.JSON:
        render(
            [g.model_dump(by_alias=True, mode="json") for g in groups],
            ctx=app_ctx_out,
            title="vpc sg list",
        )
        return
    render([_sg_row(g) for g in groups], ctx=app_ctx_out, title="vpc sg list")


@sg_app.command("show")
def sg_show(
    ctx: typer.Context, ref: str, vpc: VpcOption = None, output: OutputOption = None
) -> None:
    """Muestra un security group: sus reglas de ingress y egress."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    resolver = build_vpc_use_cases(app_ctx).resolver
    scope_vpc = resolver.resolve_vpc(vpc) if vpc else None
    sg = resolver.resolve_security_group(ref, vpc=scope_vpc)

    if app_ctx_out.output is OutputFormat.JSON:
        render(sg.model_dump(by_alias=True, mode="json"), ctx=app_ctx_out, title="vpc sg show")
        return

    app_ctx_out.console.print(f"[bold]{sg.display_name}[/] ({sg.group_id}) -- {sg.description}")
    rows = [_permission_row("ingress", p, colored=True) for p in sg.ingress]
    rows.extend(_permission_row("egress", p, colored=True) for p in sg.egress)
    render(rows, ctx=app_ctx_out, title="Reglas")


@sg_app.command("audit")
def sg_audit(
    ctx: typer.Context,
    vpc: VpcOption = None,
    min_severity: Annotated[
        Severity, typer.Option("--min-severity", help="Severidad mínima a incluir.")
    ] = Severity.INFO,
    fail_on_findings: Annotated[
        bool,
        typer.Option(
            "--fail-on-findings",
            help="Termina con exit code 3 si hay hallazgos >= --min-severity (útil en CI).",
        ),
    ] = False,
    output: OutputOption = None,
) -> None:
    """Audita los security groups contra el motor de reglas de seguridad.

    Sin --fail-on-findings, siempre termina con exit code 0 -- es una
    consulta, nunca falla por lo que encuentra. Con --fail-on-findings,
    termina con exit code 3 si hay al menos un hallazgo de severidad
    >= --min-severity, para usarlo como gate en un pipeline de CI.
    """
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    use_cases = build_vpc_use_cases(app_ctx)
    vpc_id = use_cases.resolver.resolve_vpc(vpc).vpc_id if vpc else None
    findings = use_cases.audit_security_groups.execute(
        AuditSecurityGroupsRequest(vpc_id=vpc_id, min_severity=min_severity)
    )

    if app_ctx_out.output is OutputFormat.JSON:
        render([_finding_json(f) for f in findings], ctx=app_ctx_out, title="vpc sg audit")
    else:
        render(
            [_finding_row(f, colored=True) for f in findings], ctx=app_ctx_out, title="vpc sg audit"
        )
        _print_severity_summary(app_ctx_out, findings)

    if fail_on_findings and findings:
        raise typer.Exit(code=_FINDINGS_EXIT_CODE)


def _print_severity_summary(app_ctx: AppContext, findings: list[SecurityFinding]) -> None:
    counts: dict[Severity, int] = dict.fromkeys(Severity, 0)
    for finding in findings:
        counts[finding.severity] += 1
    parts = [
        f"[{_SEVERITY_COLOR[severity]}]{severity.value}: {count}[/]"
        for severity, count in counts.items()
        if count
    ]
    summary = ", ".join(parts) if parts else "sin hallazgos"
    app_ctx.console.print(f"Resumen: {summary} (total: {len(findings)})")


# == Availability zones ========================================================


@az_app.command("list")
def az_list(ctx: typer.Context, output: OutputOption = None) -> None:
    """Lista las availability zones de la región configurada."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    use_case = build_vpc_use_cases(app_ctx).list_availability_zones
    zones = use_case.execute()
    render(
        [z.model_dump(by_alias=True, mode="json") for z in zones],
        ctx=app_ctx_out,
        title="vpc az list",
    )


# == Resolve (diagnostic) ======================================================


@vpc_app.command("resolve")
def vpc_resolve(
    ctx: typer.Context,
    vpc: VpcOption = None,
    subnet: Annotated[
        list[str] | None,
        typer.Option("--subnet", help="Referencia de subnet a resolver (repetible)."),
    ] = None,
    sg: Annotated[
        list[str] | None,
        typer.Option("--sg", help="Referencia de security group a resolver (repetible)."),
    ] = None,
    output: OutputOption = None,
) -> None:
    """Muestra a qué ID resuelve cada referencia. Diagnóstico previo a lanzar una instancia."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    resolver = build_vpc_use_cases(app_ctx).resolver
    resolved_vpc = resolver.resolve_vpc(vpc)

    rows: list[dict[str, Any]] = [
        {"Type": "vpc", "Ref": vpc or "(default)", "ResolvedId": resolved_vpc.vpc_id}
    ]
    for ref in subnet or []:
        resolved = resolver.resolve_subnet(ref, vpc=resolved_vpc)
        rows.append({"Type": "subnet", "Ref": ref, "ResolvedId": resolved.subnet_id})
    for ref in sg or []:
        resolved_sg = resolver.resolve_security_group(ref, vpc=resolved_vpc)
        rows.append({"Type": "sg", "Ref": ref, "ResolvedId": resolved_sg.group_id})

    render(rows, ctx=app_ctx_out, title="vpc resolve")
