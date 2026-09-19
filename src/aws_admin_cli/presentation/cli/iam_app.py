"""IAM CLI commands: users, policies, and roles.

Every command follows the same shape: pull ``AppContext`` off ``ctx.obj``,
call ``build_iam_use_cases(app_ctx)`` (see ``presentation/wiring.py``) to get
every IAM use case already wired to a shared gateway, build a request DTO
from the parsed CLI arguments, call ``.execute()``, and hand the result to
``render()``. No business rule lives here -- if it looks like an ``if``
enforcing a rule rather than just shaping arguments, it belongs in the use
case instead.

Table vs. JSON output is deliberately asymmetric: JSON mode always returns the
full ``model_dump(by_alias=True)`` (every field AWS gave us), while table mode
shows only a handful of the most useful columns -- a 12-column terminal table
is not "legible". See ``_render_model``/``_render_model_list`` below.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from pydantic import BaseModel

from aws_admin_cli.application.dto.iam import (
    AttachPolicyRequest,
    AttachRoleToProfileRequest,
    CreateInstanceProfileRequest,
    CreatePolicyRequest,
    CreateRoleRequest,
    CreateUserRequest,
    DeleteInstanceProfileRequest,
    DeletePolicyRequest,
    DeleteRoleRequest,
    DeleteUserRequest,
    DetachPolicyRequest,
)
from aws_admin_cli.application.services.iam_audit_metadata import (
    DISABLED_AT,
    SEED_LAST_API_ACTIVITY,
    SEED_LAST_CONSOLE_LOGIN,
    write_user_metadata,
)
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.enums import OutputFormat
from aws_admin_cli.core.exceptions import ConfigurationError, ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.iam import PolicyDocument, sanitize_path, service_trust_policy
from aws_admin_cli.presentation.cli.callbacks import OutputOption, apply_output_override
from aws_admin_cli.presentation.cli.confirm import confirm_destructive
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.wiring import build_iam_use_cases

iam_app = typer.Typer(help="Gestión de IAM.", no_args_is_help=True)
user_app = typer.Typer(help="Gestión de usuarios IAM.", no_args_is_help=True)
policy_app = typer.Typer(help="Gestión de políticas IAM.", no_args_is_help=True)
role_app = typer.Typer(help="Gestión de roles IAM.", no_args_is_help=True)
instance_profile_app = typer.Typer(
    help="Gestión de instance profiles de IAM (el contenedor que EC2 adjunta).",
    no_args_is_help=True,
)
iam_app.add_typer(user_app, name="user")
iam_app.add_typer(policy_app, name="policy")
iam_app.add_typer(role_app, name="role")
iam_app.add_typer(instance_profile_app, name="instance-profile")


# -- Shared option types ------------------------------------------------------

PathOption = Annotated[str, typer.Option("--path", help="Path de IAM.")]


def _normalize_path_prefix(value: str | None) -> str | None:
    """Wrap a bare ``--path-prefix`` (e.g. ``Admin``) in slashes before it reaches AWS.

    IAM's ``PathPrefix`` must start and end with ``"/"``; without this, a
    prefix typed without slashes would round-trip to AWS only to be
    rejected there instead of failing fast/clearly here.
    """
    return sanitize_path(value) if value else value


PathPrefixOption = Annotated[
    str | None,
    typer.Option(
        "--path-prefix", help="Filtra por prefijo de path.", callback=_normalize_path_prefix
    ),
]
ForceOption = Annotated[
    bool, typer.Option("--force", help="Desadjunta políticas y/o ignora conflictos.")
]
YesOption = Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")]
PolicyArnOption = Annotated[str, typer.Option("--policy-arn", help="ARN de la política.")]
TagOption = Annotated[
    list[str] | None,
    typer.Option("--tag", help="Tag CLAVE=VALOR (repetible)."),
]


# -- Small, shared helpers ----------------------------------------------------


def _tags_to_dict(tags: list[str] | None) -> dict[str, str]:
    """Parse repeated ``--tag KEY=VALUE`` options into a dict.

    Raises:
        ValidationError: One of the values has no ``=``.
    """
    result: dict[str, str] = {}
    for raw in tags or []:
        if "=" not in raw:
            raise ValidationError(
                f"Formato de --tag inválido: '{raw}'.",
                hint="Usa --tag CLAVE=VALOR (por ejemplo --tag Owner=ruben).",
            )
        key, _, value = raw.partition("=")
        result[key] = value
    return result


def _resolve_policy_document(
    document_file: Path | None, document_json: str | None
) -> PolicyDocument:
    """Resolve exactly one of ``--document-file``/``--document-json`` into a ``PolicyDocument``."""
    if document_file is not None and document_json is not None:
        raise ValidationError(
            "No puedes usar --document-file y --document-json a la vez.",
            hint="Elige uno de los dos.",
        )
    if document_file is not None:
        return PolicyDocument.from_aws(document_file.read_text(encoding="utf-8"))
    if document_json is not None:
        return PolicyDocument.from_aws(document_json)
    raise ValidationError(
        "Debes especificar --document-file o --document-json.",
        hint="Pasa una ruta con --document-file, o el JSON inline con --document-json.",
    )


def _resolve_trust_policy(trust_policy_file: Path | None, service: str | None) -> PolicyDocument:
    """Resolve exactly one of ``--trust-policy-file``/``--service`` into a ``PolicyDocument``."""
    if trust_policy_file is not None and service is not None:
        raise ValidationError(
            "No puedes usar --trust-policy-file y --service a la vez.",
            hint="Elige uno de los dos.",
        )
    if service is not None:
        return service_trust_policy(service)
    if trust_policy_file is not None:
        return PolicyDocument.from_aws(trust_policy_file.read_text(encoding="utf-8"))
    raise ValidationError(
        "Debes especificar --trust-policy-file o --service.",
        hint="Usa --service ec2.amazonaws.com, o --trust-policy-file para un trust "
        "policy personalizado.",
    )


def _render_model(
    model: BaseModel,
    *,
    ctx: AppContext,
    output: OutputFormat | None,
    table_fields: Sequence[str],
    title: str,
) -> None:
    """Render a single model: full fields as JSON (a bare object), a column subset as a table.

    Args:
        model: The single resource to render (``create``/``get`` commands).
        ctx: The command's ``AppContext`` (before any ``--output`` override).
        output: This command's own ``--output``, if passed after the subcommand name.
        table_fields: PascalCase (aliased) field names to show as table columns.
            Ignored in JSON mode.
        title: Table title. Ignored in JSON mode.
    """
    app_ctx = apply_output_override(ctx, output)
    dumped = model.model_dump(by_alias=True, mode="json")
    if app_ctx.output is OutputFormat.JSON:
        render(dumped, ctx=app_ctx, title=title)
        return
    row: dict[str, Any] = {field: dumped[field] for field in table_fields if field in dumped}
    render(row, ctx=app_ctx, title=title)


def _render_model_list(
    models: Sequence[BaseModel],
    *,
    ctx: AppContext,
    output: OutputFormat | None,
    table_fields: Sequence[str],
    title: str,
) -> None:
    """Render several models: full fields as JSON (an array), a column subset as a table.

    Args:
        models: Zero or more resources to render (``list`` commands).
        ctx: The command's ``AppContext`` (before any ``--output`` override).
        output: This command's own ``--output``, if passed after the subcommand name.
        table_fields: PascalCase (aliased) field names to show as table columns.
            Ignored in JSON mode.
        title: Table title. Ignored in JSON mode.
    """
    app_ctx = apply_output_override(ctx, output)
    dumped = [model.model_dump(by_alias=True, mode="json") for model in models]
    if app_ctx.output is OutputFormat.JSON:
        render(dumped, ctx=app_ctx, title=title)
        return
    rows: list[dict[str, Any]] = [
        {field: row[field] for field in table_fields if field in row} for row in dumped
    ]
    render(rows, ctx=app_ctx, title=title)


_USER_TABLE_FIELDS = ("UserName", "UserId", "Arn", "CreateDate")
_POLICY_TABLE_FIELDS = ("PolicyName", "Arn", "AttachmentCount", "DefaultVersionId")
_ROLE_TABLE_FIELDS = ("RoleName", "Arn", "MaxSessionDuration", "CreateDate")
_ATTACHED_POLICY_TABLE_FIELDS = ("PolicyName", "PolicyArn")
_INSTANCE_PROFILE_TABLE_FIELDS = ("InstanceProfileName", "Arn", "Path", "CreateDate")


def _instance_profile_row(instance_profile: Any) -> dict[str, Any]:
    dumped = instance_profile.model_dump(by_alias=True, mode="json")
    dumped["RoleName"] = instance_profile.role_name
    fields = (*_INSTANCE_PROFILE_TABLE_FIELDS, "RoleName")
    return {field: dumped[field] for field in fields if field in dumped}


# == Users ====================================================================


@user_app.command("create")
def user_create(
    ctx: typer.Context,
    name: str,
    path: PathOption = "/",
    tag: TagOption = None,
    if_not_exists: Annotated[
        bool, typer.Option("--if-not-exists", help="No falla si el usuario ya existe.")
    ] = False,
    output: OutputOption = None,
) -> None:
    """Crea un usuario de IAM."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).create_user
    user = use_case.execute(
        CreateUserRequest(
            name=name, path=path, tags=_tags_to_dict(tag), if_not_exists=if_not_exists
        )
    )
    _render_model(
        user, ctx=app_ctx, output=output, table_fields=_USER_TABLE_FIELDS, title="user create"
    )


@user_app.command("list")
def user_list(
    ctx: typer.Context, path_prefix: PathPrefixOption = None, output: OutputOption = None
) -> None:
    """Lista usuarios de IAM."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).list_users
    users = use_case.execute(path_prefix)
    _render_model_list(
        users, ctx=app_ctx, output=output, table_fields=_USER_TABLE_FIELDS, title="user list"
    )


@user_app.command("get")
def user_get(ctx: typer.Context, name: str, output: OutputOption = None) -> None:
    """Muestra un usuario de IAM."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).get_user
    user = use_case.execute(name)
    _render_model(
        user, ctx=app_ctx, output=output, table_fields=_USER_TABLE_FIELDS, title="user get"
    )


@user_app.command("delete")
def user_delete(
    ctx: typer.Context,
    name: str,
    force: ForceOption = False,
    yes: YesOption = False,
) -> None:
    """Borra un usuario de IAM."""
    app_ctx: AppContext = ctx.obj
    confirm_destructive(app_ctx, action="borrar", target=f"el usuario '{name}'", assume_yes=yes)
    use_case = build_iam_use_cases(app_ctx).delete_user
    use_case.execute(DeleteUserRequest(name=name, force=force))
    app_ctx.console.print(f"[green]Usuario '{name}' borrado.[/]")


@user_app.command("attach-policy")
def user_attach_policy(ctx: typer.Context, name: str, policy_arn: PolicyArnOption) -> None:
    """Adjunta una política a un usuario."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).attach_policy
    use_case.execute(
        AttachPolicyRequest(principal_name=name, policy_arn=policy_arn, principal_type="user")
    )
    app_ctx.console.print(f"[green]Política adjuntada a '{name}'.[/]")


@user_app.command("detach-policy")
def user_detach_policy(ctx: typer.Context, name: str, policy_arn: PolicyArnOption) -> None:
    """Desadjunta una política de un usuario."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).detach_policy
    use_case.execute(
        DetachPolicyRequest(principal_name=name, policy_arn=policy_arn, principal_type="user")
    )
    app_ctx.console.print(f"[green]Política desadjuntada de '{name}'.[/]")


@user_app.command("policies")
def user_policies(ctx: typer.Context, name: str, output: OutputOption = None) -> None:
    """Lista las políticas adjuntas a un usuario."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).list_attached_policies
    policies = use_case.execute(name, "user")
    _render_model_list(
        policies,
        ctx=app_ctx,
        output=output,
        table_fields=_ATTACHED_POLICY_TABLE_FIELDS,
        title="user policies",
    )


# == Policies =================================================================


@policy_app.command("create")
def policy_create(
    ctx: typer.Context,
    name: str,
    document_file: Annotated[
        Path | None, typer.Option("--document-file", help="Ruta a un JSON de política.")
    ] = None,
    document_json: Annotated[
        str | None, typer.Option("--document-json", help="Política como JSON inline.")
    ] = None,
    description: Annotated[str | None, typer.Option("--description")] = None,
    path: PathOption = "/",
    allow_wildcard: Annotated[
        bool,
        typer.Option("--allow-wildcard", help='Permite Allow "*" sobre Resource "*".'),
    ] = False,
    output: OutputOption = None,
) -> None:
    """Crea una política de IAM administrada por el cliente."""
    app_ctx: AppContext = ctx.obj
    document = _resolve_policy_document(document_file, document_json)
    use_case = build_iam_use_cases(app_ctx).create_policy
    policy = use_case.execute(
        CreatePolicyRequest(
            name=name,
            document=document,
            path=path,
            description=description,
            allow_wildcard=allow_wildcard,
        )
    )
    _render_model(
        policy,
        ctx=app_ctx,
        output=output,
        table_fields=_POLICY_TABLE_FIELDS,
        title="policy create",
    )


@policy_app.command("list")
def policy_list(
    ctx: typer.Context,
    scope: Annotated[str, typer.Option("--scope", help="Local | AWS | All.")] = "All",
    only_attached: Annotated[bool, typer.Option("--only-attached")] = False,
    output: OutputOption = None,
) -> None:
    """Lista políticas de IAM."""
    app_ctx: AppContext = ctx.obj
    if scope not in ("Local", "AWS", "All"):
        raise ValidationError(f"--scope inválido: '{scope}'.", hint="Usa Local, AWS o All.")
    typed_scope: Literal["All", "AWS", "Local"] = scope  # type: ignore[assignment]
    use_case = build_iam_use_cases(app_ctx).list_policies
    policies = use_case.execute(typed_scope, only_attached)
    _render_model_list(
        policies, ctx=app_ctx, output=output, table_fields=_POLICY_TABLE_FIELDS, title="policy list"
    )


@policy_app.command("get")
def policy_get(
    ctx: typer.Context,
    arn: str,
    show_document: Annotated[bool, typer.Option("--show-document")] = False,
    output: OutputOption = None,
) -> None:
    """Muestra una política de IAM."""
    app_ctx: AppContext = ctx.obj
    use_cases = build_iam_use_cases(app_ctx)
    policy = use_cases.get_policy.execute(arn)
    if not show_document:
        _render_model(
            policy,
            ctx=app_ctx,
            output=output,
            table_fields=_POLICY_TABLE_FIELDS,
            title="policy get",
        )
        return

    document = use_cases.get_policy_document.execute(arn, policy.default_version_id)
    app_ctx_out = apply_output_override(app_ctx, output)
    payload = policy.model_dump(by_alias=True, mode="json")
    payload["Document"] = json.loads(document.to_aws_json())
    render(payload, ctx=app_ctx_out, title="policy get")


@policy_app.command("delete")
def policy_delete(
    ctx: typer.Context,
    arn: str,
    force: ForceOption = False,
    yes: YesOption = False,
) -> None:
    """Borra una política de IAM."""
    app_ctx: AppContext = ctx.obj
    confirm_destructive(app_ctx, action="borrar", target=f"la política '{arn}'", assume_yes=yes)
    use_case = build_iam_use_cases(app_ctx).delete_policy
    use_case.execute(DeletePolicyRequest(arn=arn, force=force))
    app_ctx.console.print(f"[green]Política '{arn}' borrada.[/]")


# == Roles ====================================================================


@role_app.command("create")
def role_create(
    ctx: typer.Context,
    name: str,
    trust_policy_file: Annotated[
        Path | None, typer.Option("--trust-policy-file", help="Ruta a un trust policy JSON.")
    ] = None,
    service: Annotated[
        str | None,
        typer.Option("--service", help="Principal de servicio, p. ej. ec2.amazonaws.com."),
    ] = None,
    description: Annotated[str | None, typer.Option("--description")] = None,
    max_session_duration: Annotated[int | None, typer.Option("--max-session-duration")] = None,
    path: PathOption = "/",
    if_not_exists: Annotated[
        bool, typer.Option("--if-not-exists", help="No falla si el rol ya existe.")
    ] = False,
    output: OutputOption = None,
) -> None:
    """Crea un rol de IAM."""
    app_ctx: AppContext = ctx.obj
    trust_policy = _resolve_trust_policy(trust_policy_file, service)
    use_case = build_iam_use_cases(app_ctx).create_role
    role = use_case.execute(
        CreateRoleRequest(
            name=name,
            trust_policy=trust_policy,
            path=path,
            description=description,
            max_session_duration=max_session_duration,
            if_not_exists=if_not_exists,
        )
    )
    _render_model(
        role, ctx=app_ctx, output=output, table_fields=_ROLE_TABLE_FIELDS, title="role create"
    )


@role_app.command("list")
def role_list(
    ctx: typer.Context, path_prefix: PathPrefixOption = None, output: OutputOption = None
) -> None:
    """Lista roles de IAM."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).list_roles
    roles = use_case.execute(path_prefix)
    _render_model_list(
        roles, ctx=app_ctx, output=output, table_fields=_ROLE_TABLE_FIELDS, title="role list"
    )


@role_app.command("get")
def role_get(
    ctx: typer.Context,
    name: str,
    show_trust_policy: Annotated[bool, typer.Option("--show-trust-policy")] = False,
    output: OutputOption = None,
) -> None:
    """Muestra un rol de IAM."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).get_role
    role = use_case.execute(name)
    if not show_trust_policy:
        _render_model(
            role, ctx=app_ctx, output=output, table_fields=_ROLE_TABLE_FIELDS, title="role get"
        )
        return

    app_ctx_out = apply_output_override(app_ctx, output)
    payload = role.model_dump(by_alias=True, mode="json")
    render(payload, ctx=app_ctx_out, title="role get")


@role_app.command("delete")
def role_delete(
    ctx: typer.Context,
    name: str,
    force: ForceOption = False,
    yes: YesOption = False,
) -> None:
    """Borra un rol de IAM."""
    app_ctx: AppContext = ctx.obj
    confirm_destructive(app_ctx, action="borrar", target=f"el rol '{name}'", assume_yes=yes)
    use_case = build_iam_use_cases(app_ctx).delete_role
    use_case.execute(DeleteRoleRequest(name=name, force=force))
    app_ctx.console.print(f"[green]Rol '{name}' borrado.[/]")


@role_app.command("attach-policy")
def role_attach_policy(ctx: typer.Context, name: str, policy_arn: PolicyArnOption) -> None:
    """Adjunta una política a un rol."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).attach_policy
    use_case.execute(
        AttachPolicyRequest(principal_name=name, policy_arn=policy_arn, principal_type="role")
    )
    app_ctx.console.print(f"[green]Política adjuntada a '{name}'.[/]")


@role_app.command("detach-policy")
def role_detach_policy(ctx: typer.Context, name: str, policy_arn: PolicyArnOption) -> None:
    """Desadjunta una política de un rol."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).detach_policy
    use_case.execute(
        DetachPolicyRequest(principal_name=name, policy_arn=policy_arn, principal_type="role")
    )
    app_ctx.console.print(f"[green]Política desadjuntada de '{name}'.[/]")


@role_app.command("policies")
def role_policies(ctx: typer.Context, name: str, output: OutputOption = None) -> None:
    """Lista las políticas adjuntas a un rol."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).list_attached_policies
    policies = use_case.execute(name, "role")
    _render_model_list(
        policies,
        ctx=app_ctx,
        output=output,
        table_fields=_ATTACHED_POLICY_TABLE_FIELDS,
        title="role policies",
    )


# == Instance profiles ========================================================


@instance_profile_app.command("create")
def instance_profile_create(
    ctx: typer.Context,
    name: str,
    path: PathOption = "/",
    if_not_exists: Annotated[
        bool, typer.Option("--if-not-exists", help="No falla si el instance profile ya existe.")
    ] = False,
    output: OutputOption = None,
) -> None:
    """Crea un instance profile de IAM (el contenedor que EC2 adjunta a una instancia)."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).create_instance_profile
    instance_profile = use_case.execute(
        CreateInstanceProfileRequest(name=name, path=path, if_not_exists=if_not_exists)
    )
    app_ctx_out = apply_output_override(app_ctx, output)
    if app_ctx_out.output is OutputFormat.JSON:
        render(
            instance_profile.model_dump(by_alias=True, mode="json"),
            ctx=app_ctx_out,
            title="instance-profile create",
        )
        return
    render(
        _instance_profile_row(instance_profile), ctx=app_ctx_out, title="instance-profile create"
    )


@instance_profile_app.command("list")
def instance_profile_list(
    ctx: typer.Context, path_prefix: PathPrefixOption = None, output: OutputOption = None
) -> None:
    """Lista instance profiles de IAM."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).list_instance_profiles
    profiles = use_case.execute(path_prefix)
    app_ctx_out = apply_output_override(app_ctx, output)
    if app_ctx_out.output is OutputFormat.JSON:
        render(
            [p.model_dump(by_alias=True, mode="json") for p in profiles],
            ctx=app_ctx_out,
            title="instance-profile list",
        )
        return
    render(
        [_instance_profile_row(p) for p in profiles], ctx=app_ctx_out, title="instance-profile list"
    )


@instance_profile_app.command("show")
def instance_profile_show(ctx: typer.Context, name: str, output: OutputOption = None) -> None:
    """Muestra un instance profile de IAM, incluido su rol adjunto (si tiene)."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).get_instance_profile
    instance_profile = use_case.execute(name)
    app_ctx_out = apply_output_override(app_ctx, output)
    if app_ctx_out.output is OutputFormat.JSON:
        render(
            instance_profile.model_dump(by_alias=True, mode="json"),
            ctx=app_ctx_out,
            title="instance-profile show",
        )
        return
    render(_instance_profile_row(instance_profile), ctx=app_ctx_out, title="instance-profile show")


@instance_profile_app.command("delete")
def instance_profile_delete(
    ctx: typer.Context, name: str, force: ForceOption = False, yes: YesOption = False
) -> None:
    """Borra un instance profile de IAM."""
    app_ctx: AppContext = ctx.obj
    confirm_destructive(
        app_ctx, action="borrar", target=f"el instance profile '{name}'", assume_yes=yes
    )
    use_case = build_iam_use_cases(app_ctx).delete_instance_profile
    use_case.execute(DeleteInstanceProfileRequest(name=name, force=force))
    app_ctx.console.print(f"[green]Instance profile '{name}' borrado.[/]")


@instance_profile_app.command("attach-role")
def instance_profile_attach_role(
    ctx: typer.Context, name: str, role_name: Annotated[str, typer.Option("--role-name")]
) -> None:
    """Adjunta un rol a un instance profile (idempotente: no falla si ya está adjunto)."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).attach_role_to_profile
    use_case.execute(AttachRoleToProfileRequest(profile_name=name, role_name=role_name))
    app_ctx.console.print(f"[green]Rol '{role_name}' adjuntado a '{name}'.[/]")


@instance_profile_app.command("detach-role")
def instance_profile_detach_role(
    ctx: typer.Context, name: str, role_name: Annotated[str, typer.Option("--role-name")]
) -> None:
    """Desadjunta un rol de un instance profile."""
    app_ctx: AppContext = ctx.obj
    use_case = build_iam_use_cases(app_ctx).detach_role_from_profile
    use_case.execute(AttachRoleToProfileRequest(profile_name=name, role_name=role_name))
    app_ctx.console.print(f"[green]Rol '{role_name}' desadjuntado de '{name}'.[/]")


# == Dev/Testing: seed data for "Audit Inactive Users" (LocalStack only) ============
#
# AWS never lets a caller backdate `PasswordLastUsed`/`AccessKeyLastUsed`, nor set
# when a policy was attached -- there is no real API path to manufacture "this user
# has been idle for 17 days" data. These 5 fake users get their audit-relevant
# timestamps written straight into the local `ResourceRecord` ledger instead (see
# `application/services/iam_audit_metadata.py`), which is exactly what the TUI's
# Dormant/Disabled audit views read. Gated on `is_local` below: doing this against a
# real account would plant fake users with fabricated activity history in it.


@dataclass(frozen=True, slots=True)
class _SeedUser:
    """One fake user ``seed-audit-users`` creates, and the audit signals it fabricates."""

    name: str
    last_console_days_ago: int
    last_api_days_ago: int
    disabled_days_ago: int | None = None  # None = not disabled


_SEED_USERS: tuple[_SeedUser, ...] = (
    # Dormant (15-20 days idle), not disabled -- both must show up under "🟡 Dormant".
    _SeedUser("Carlos_Mendoza", last_console_days_ago=17, last_api_days_ago=18),
    _SeedUser("Ana_Gomez", last_console_days_ago=20, last_api_days_ago=15),
    # Disabled >= 30 days -- must show up under "🔴 Disabled".
    _SeedUser(
        "Roberto_Silva", last_console_days_ago=60, last_api_days_ago=60, disabled_days_ago=45
    ),
    # Disabled < 30 days -- must NOT show up under "🔴 Disabled".
    _SeedUser("Elena_Torres", last_console_days_ago=40, last_api_days_ago=40, disabled_days_ago=10),
    # Active today/yesterday -- must NOT show up in either audit view.
    _SeedUser("Sofia_Rojas", last_console_days_ago=0, last_api_days_ago=1),
)


def _delete_seed_user_if_exists(app_ctx: AppContext, name: str) -> None:
    use_cases = build_iam_use_cases(app_ctx)
    try:
        use_cases.get_user.execute(name)
    except ResourceNotFoundError:
        return
    use_cases.delete_user.execute(DeleteUserRequest(name=name, force=True))


def _seed_one_user(app_ctx: AppContext, spec: _SeedUser) -> None:
    use_cases = build_iam_use_cases(app_ctx)
    now = datetime.now(UTC)
    user = use_cases.create_user.execute(CreateUserRequest(name=spec.name))
    updates: dict[str, datetime | None] = {
        SEED_LAST_CONSOLE_LOGIN: now - timedelta(days=spec.last_console_days_ago),
        SEED_LAST_API_ACTIVITY: now - timedelta(days=spec.last_api_days_ago),
    }
    if spec.disabled_days_ago is not None:
        policy_arn = use_cases.resolve_deny_all_policy.execute()
        use_cases.attach_policy.execute(
            AttachPolicyRequest(
                principal_name=user.user_name, policy_arn=policy_arn, principal_type="user"
            )
        )
        updates[DISABLED_AT] = now - timedelta(days=spec.disabled_days_ago)
    write_user_metadata(
        app_ctx.resource_repository,
        user=user,
        profile=app_ctx.settings.profile,
        region=app_ctx.settings.region,
        updates=updates,
    )


@user_app.command("seed-audit-users")
def user_seed_audit_users(
    ctx: typer.Context,
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Borra los usuarios de prueba en vez de crearlos."),
    ] = False,
) -> None:
    """Crea (o borra, con --clean) 5 usuarios ficticios para probar Audit Inactive Users.

    Se niega a correr contra una cuenta AWS real: fabrica fechas de actividad y de
    deshabilitado retroactivas, algo que solo tiene sentido contra LocalStack.
    """
    app_ctx: AppContext = ctx.obj
    if not app_ctx.settings.is_local:
        raise ConfigurationError(
            "seed-audit-users solo puede ejecutarse contra LocalStack.",
            hint="Usa un perfil/--endpoint-url que apunte a LocalStack (ver docs/aws-profiles.md).",
        )

    if clean:
        for spec in _SEED_USERS:
            _delete_seed_user_if_exists(app_ctx, spec.name)
            app_ctx.console.print(f"[green]'{spec.name}' borrado (si existía).[/]")
        return

    for spec in _SEED_USERS:
        _delete_seed_user_if_exists(app_ctx, spec.name)  # clean slate: deterministic re-seed
        _seed_one_user(app_ctx, spec)
        app_ctx.console.print(f"[green]'{spec.name}' sembrado.[/]")
