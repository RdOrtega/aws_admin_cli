"""EC2 CLI commands: AMIs, key pairs, and instance lifecycle.

Same shape as ``iam_app.py``/``s3_app.py``/``vpc_app.py``: pull ``AppContext``
off ``ctx.obj``, build gateways/resolvers/use cases, call ``.execute()``, hand
the result to ``render()``. No business rule lives here.

``ec2 instance launch`` and every destructive-adjacent instance command
(``stop``/``reboot``/``terminate``) resolve human references FIRST (so the
confirmation prompt shows what's actually about to happen, by real ID -- not
just echo back the raw string the user typed) and route through
``confirm_destructive`` unless ``--yes``, same as every other module.
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
from pydantic import BaseModel

from aws_admin_cli.application.dto.ec2 import (
    CopyAmiRequest,
    CreateAmiRequest,
    CreateKeyPairRequest,
    LaunchInstanceRequest,
    ListInstancesRequest,
)
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.enums import OutputFormat
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.ec2 import Ami, Instance, get_instance_spec
from aws_admin_cli.presentation.cli.callbacks import OutputOption, apply_output_override
from aws_admin_cli.presentation.cli.confirm import confirm_destructive
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.tui.flows._shared import run_with_spinner
from aws_admin_cli.presentation.wiring import build_ec2_use_cases

ec2_app = typer.Typer(help="Gestión de EC2 (AMIs, key pairs, instancias).", no_args_is_help=True)
ami_app = typer.Typer(help="AMIs.", no_args_is_help=True)
keypair_app = typer.Typer(help="Key pairs.", no_args_is_help=True)
instance_app = typer.Typer(help="Instancias.", no_args_is_help=True)
ec2_app.add_typer(ami_app, name="ami")
ec2_app.add_typer(keypair_app, name="keypair")
ec2_app.add_typer(instance_app, name="instance")

_DEFAULT_TIMEOUT_S = 300

YesOption = Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")]
ForceOption = Annotated[bool, typer.Option("--force", help="Ignora la salvaguarda ManagedBy.")]
_T = TypeVar("_T")


# -- Small, shared helpers ----------------------------------------------------


def _tags_to_dict(tags: list[str] | None) -> dict[str, str]:
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


def _run_with_spinner(app_ctx: AppContext, description: str, action: Callable[[], _T]) -> _T:
    """Run ``action`` under a spinner on STDERR -- disabled in JSON mode or without a TTY.

    Delegates the actual spinner to ``flows._shared.run_with_spinner`` (shared
    with the TUI layer); JSON mode is a CLI-only concern, so it short-circuits
    here without ever constructing a spinner.
    """
    if app_ctx.output is OutputFormat.JSON:
        return action()
    return run_with_spinner(app_ctx.err_console, description, action)


def _render_model(
    model: BaseModel,
    *,
    ctx: AppContext,
    output: OutputFormat | None,
    table_fields: Sequence[str],
    title: str,
) -> None:
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
    app_ctx = apply_output_override(ctx, output)
    dumped = [model.model_dump(by_alias=True, mode="json") for model in models]
    if app_ctx.output is OutputFormat.JSON:
        render(dumped, ctx=app_ctx, title=title)
        return
    rows = [{field: row[field] for field in table_fields if field in row} for row in dumped]
    render(rows, ctx=app_ctx, title=title)


_AMI_TABLE_FIELDS = ("ImageId", "Name", "Architecture", "CreationDate", "Public")
_KEYPAIR_TABLE_FIELDS = ("KeyName", "KeyPairId", "KeyFingerprint", "KeyType")
_INSTANCE_TABLE_FIELDS = (
    "InstanceId",
    "Name",
    "InstanceType",
    "State",
    "PrivateIpAddress",
    "PublicIpAddress",
    "SubnetId",
    "ManagedByCli",
)


def _instance_row(instance: Instance) -> dict[str, Any]:
    dumped = instance.model_dump(by_alias=True, mode="json")
    dumped["Name"] = instance.name
    dumped["ManagedByCli"] = instance.managed_by_cli
    return {field: dumped[field] for field in _INSTANCE_TABLE_FIELDS if field in dumped}


def _render_instance(
    instance: Instance, *, ctx: AppContext, output: OutputFormat | None = None, title: str
) -> None:
    app_ctx = apply_output_override(ctx, output)
    if app_ctx.output is OutputFormat.JSON:
        render(instance.model_dump(by_alias=True, mode="json"), ctx=app_ctx, title=title)
        return
    render(_instance_row(instance), ctx=app_ctx, title=title)


def _render_instance_list(
    instances: Sequence[Instance], *, ctx: AppContext, output: OutputFormat | None, title: str
) -> None:
    app_ctx = apply_output_override(ctx, output)
    if app_ctx.output is OutputFormat.JSON:
        render(
            [i.model_dump(by_alias=True, mode="json") for i in instances], ctx=app_ctx, title=title
        )
        return
    render([_instance_row(i) for i in instances], ctx=app_ctx, title=title)


# == AMIs ======================================================================


@ami_app.command("list")
def ami_list(
    ctx: typer.Context,
    alias: Annotated[
        str | None, typer.Option("--alias", help="Alias conocido (ver `ami resolve`).")
    ] = None,
    owner: Annotated[str | None, typer.Option("--owner")] = None,
    name_pattern: Annotated[str | None, typer.Option("--name-pattern")] = None,
    output: OutputOption = None,
) -> None:
    """Lista AMIs. --alias devuelve solo la más reciente que resuelve ese alias."""
    app_ctx: AppContext = ctx.obj
    wiring = build_ec2_use_cases(app_ctx)
    if alias:
        ami = wiring.ami_resolver.resolve(alias)
        _render_model_list(
            [ami], ctx=app_ctx, output=output, table_fields=_AMI_TABLE_FIELDS, title="ami list"
        )
        return
    amis = wiring.list_amis.execute(owner=owner, name_pattern=name_pattern)
    _render_model_list(
        amis, ctx=app_ctx, output=output, table_fields=_AMI_TABLE_FIELDS, title="ami list"
    )


@ami_app.command("resolve")
def ami_resolve(ctx: typer.Context, ref: str, output: OutputOption = None) -> None:
    """Muestra a qué AMI resuelve una referencia (id, alias, o nombre exacto)."""
    app_ctx: AppContext = ctx.obj
    ami = build_ec2_use_cases(app_ctx).ami_resolver.resolve(ref)
    _render_model(
        ami, ctx=app_ctx, output=output, table_fields=_AMI_TABLE_FIELDS, title="ami resolve"
    )


@ami_app.command("create")
def ami_create(
    ctx: typer.Context,
    instance_ref: Annotated[str, typer.Argument(help="instance-id, o Name tag.")],
    name: Annotated[str, typer.Option("--name")],
    description: Annotated[str | None, typer.Option("--description")] = None,
    no_reboot: Annotated[
        bool,
        typer.Option("--no-reboot/--reboot", help="Sin --reboot, la instancia se reinicia."),
    ] = True,
    output: OutputOption = None,
) -> None:
    """Crea una AMI a partir de una instancia existente."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    wiring = build_ec2_use_cases(app_ctx)
    instance = wiring.get_instance.execute(instance_ref)
    ami: Ami = _run_with_spinner(
        app_ctx_out,
        f"Creando AMI '{name}' desde '{instance.instance_id}'...",
        lambda: wiring.create_ami.execute(
            CreateAmiRequest(
                instance_id=instance.instance_id,
                name=name,
                description=description,
                no_reboot=no_reboot,
            )
        ),
    )
    _render_model(
        ami, ctx=app_ctx_out, output=output, table_fields=_AMI_TABLE_FIELDS, title="ami create"
    )


@ami_app.command("copy")
def ami_copy(
    ctx: typer.Context,
    source_ref: Annotated[str, typer.Argument(help="ami-id, alias, o nombre exacto de origen.")],
    name: Annotated[str, typer.Option("--name")],
    target_region: Annotated[str, typer.Option("--target-region")],
    description: Annotated[str | None, typer.Option("--description")] = None,
    kms_key_id: Annotated[str | None, typer.Option("--kms-key-id")] = None,
    output: OutputOption = None,
) -> None:
    """Copia una AMI a otra región, heredando sus tags. No pide VPC/SGs (no aplica a AMIs)."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    wiring = build_ec2_use_cases(app_ctx)
    source = wiring.ami_resolver.resolve(source_ref)
    ami: Ami = _run_with_spinner(
        app_ctx_out,
        f"Copiando AMI '{source.image_id}' a '{target_region}'...",
        lambda: wiring.copy_ami.execute(
            CopyAmiRequest(
                source_image_id=source.image_id,
                name=name,
                target_region=target_region,
                description=description,
                kms_key_id=kms_key_id,
            )
        ),
    )
    _render_model(
        ami, ctx=app_ctx_out, output=output, table_fields=_AMI_TABLE_FIELDS, title="ami copy"
    )


# == Key pairs =================================================================


@keypair_app.command("create")
def keypair_create(
    ctx: typer.Context,
    name: str,
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Directorio destino del .pem (por defecto ~/.ssh)."),
    ] = None,
    key_type: Annotated[str, typer.Option("--type", help="rsa | ed25519.")] = "rsa",
    output: OutputOption = None,
) -> None:
    """Crea una key pair; el material privado se guarda en PATH/NAME.pem con permisos 0600."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    destination_dir = path if path is not None else Path.home() / ".ssh"
    use_case = build_ec2_use_cases(app_ctx).create_key_pair
    destination, info = use_case.execute(
        CreateKeyPairRequest(name=name, destination_dir=destination_dir, key_type=key_type)
    )
    # The private key material NEVER appears here -- only where it was saved.
    payload = {
        "KeyName": info.key_name,
        "KeyPairId": info.key_pair_id,
        "KeyFingerprint": info.key_fingerprint,
        "Path": str(destination),
    }
    render(payload, ctx=app_ctx_out, title="keypair create")


@keypair_app.command("list")
def keypair_list(ctx: typer.Context, output: OutputOption = None) -> None:
    """Lista key pairs (solo metadatos: nunca material privado)."""
    app_ctx: AppContext = ctx.obj
    infos = build_ec2_use_cases(app_ctx).list_key_pairs.execute()
    _render_model_list(
        infos, ctx=app_ctx, output=output, table_fields=_KEYPAIR_TABLE_FIELDS, title="keypair list"
    )


@keypair_app.command("delete")
def keypair_delete(ctx: typer.Context, name: str, yes: YesOption = False) -> None:
    """Borra una key pair de AWS (el .pem local, si existe, no se toca)."""
    app_ctx: AppContext = ctx.obj
    confirm_destructive(app_ctx, action="borrar", target=f"la key pair '{name}'", assume_yes=yes)
    build_ec2_use_cases(app_ctx).delete_key_pair.execute(name)
    app_ctx.console.print(f"[green]Key pair '{name}' borrada.[/]")


# == Instances ==================================================================


def _instance_type_summary(instance_type: str) -> str:
    spec = get_instance_spec(instance_type)
    if spec is None:
        return f"{instance_type} (fuera del catálogo conocido de esta CLI)"
    return f"{instance_type} ({spec.vcpus} vCPU, {spec.memory_gib:g} GiB, familia {spec.family})"


@instance_app.command("launch")
def instance_launch(
    ctx: typer.Context,
    name: str,
    ami: Annotated[str, typer.Option("--ami", help="ami-id, alias, o nombre exacto.")],
    instance_type: Annotated[str, typer.Option("--type")],
    subnet: Annotated[str, typer.Option("--subnet", help="Nombre o subnet-id.")],
    sg: Annotated[list[str], typer.Option("--sg", help="Nombre o sg-id (repetible).")],
    key_name: Annotated[str | None, typer.Option("--key-name")] = None,
    iam_role: Annotated[
        str | None, typer.Option("--iam-role", help="Garantiza el instance profile para este rol.")
    ] = None,
    user_data_file: Annotated[Path | None, typer.Option("--user-data-file")] = None,
    volume_size: Annotated[int, typer.Option("--volume-size")] = 8,
    volume_type: Annotated[str, typer.Option("--volume-type")] = "gp3",
    public_ip: Annotated[bool, typer.Option("--public-ip")] = False,
    confirm_public: Annotated[bool, typer.Option("--confirm-public")] = False,
    confirm_large: Annotated[bool, typer.Option("--confirm-large")] = False,
    count: Annotated[int, typer.Option("--count")] = 1,
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = False,
    timeout: Annotated[int, typer.Option("--timeout")] = _DEFAULT_TIMEOUT_S,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    client_token: Annotated[str | None, typer.Option("--client-token")] = None,
    yes: YesOption = False,
    output: OutputOption = None,
) -> None:
    """Lanza una instancia EC2. Muestra un resumen de lo resuelto y pide confirmación."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)

    wiring = build_ec2_use_cases(app_ctx)
    network_resolver = wiring.network_resolver
    ami_resolver = wiring.ami_resolver

    resolved_ami = ami_resolver.resolve(ami)
    resolved_subnet = network_resolver.resolve_subnet(subnet)
    resolved_vpc = network_resolver.resolve_vpc(resolved_subnet.vpc_id)
    resolved_sgs = network_resolver.resolve_security_groups(sg, vpc=resolved_vpc)

    if not dry_run:
        app_ctx.err_console.print("[bold]Se va a lanzar:[/]")
        app_ctx.err_console.print(f"  Nombre: {name}")
        app_ctx.err_console.print(f"  AMI: {resolved_ami.image_id} ({resolved_ami.name})")
        app_ctx.err_console.print(f"  Tipo: {_instance_type_summary(instance_type)}")
        app_ctx.err_console.print(
            f"  Subnet: {resolved_subnet.subnet_id} ({resolved_subnet.display_name}, "
            f"AZ {resolved_subnet.availability_zone})"
        )
        app_ctx.err_console.print(
            "  Security groups: "
            + ", ".join(f"{s.group_id} ({s.display_name})" for s in resolved_sgs)
        )
        if iam_role:
            app_ctx.err_console.print(f"  IAM role: {iam_role}")
        confirm_destructive(
            app_ctx, action="lanzar", target=f"la instancia '{name}'", assume_yes=yes
        )

    use_case = wiring.launch_instance
    request = LaunchInstanceRequest(
        name=name,
        ami_ref=resolved_ami.image_id,
        instance_type=instance_type,
        subnet_ref=resolved_subnet.subnet_id,
        security_group_refs=tuple(s.group_id for s in resolved_sgs),
        vpc_ref=resolved_vpc.vpc_id,
        key_name=key_name,
        iam_role=iam_role,
        user_data=user_data_file.read_text(encoding="utf-8") if user_data_file else None,
        volume_size_gb=volume_size,
        volume_type=volume_type,
        assign_public_ip=public_ip,
        confirm_public=confirm_public,
        confirm_large=confirm_large,
        count=count,
        tags=_tags_to_dict(tag),
        wait=wait,
        timeout_s=timeout,
        dry_run=dry_run,
        client_token_nonce=client_token,
    )

    if wait and not dry_run:
        instance = _run_with_spinner(
            app_ctx_out,
            f"Esperando a que '{name}' esté RUNNING...",
            lambda: use_case.execute(request),
        )
    else:
        instance = use_case.execute(request)

    _render_instance(instance, ctx=app_ctx_out, title="instance launch")


@instance_app.command("list")
def instance_list(
    ctx: typer.Context,
    state: Annotated[str | None, typer.Option("--state")] = None,
    managed_only: Annotated[bool, typer.Option("--managed-only")] = False,
    vpc: Annotated[str | None, typer.Option("--vpc")] = None,
    subnet: Annotated[str | None, typer.Option("--subnet")] = None,
    output: OutputOption = None,
) -> None:
    """Lista instancias EC2."""
    app_ctx: AppContext = ctx.obj
    use_case = build_ec2_use_cases(app_ctx).list_instances
    instances = use_case.execute(
        ListInstancesRequest(state=state, vpc_ref=vpc, subnet_ref=subnet, managed_only=managed_only)
    )
    _render_instance_list(instances, ctx=app_ctx, output=output, title="instance list")


@instance_app.command("show")
def instance_show(ctx: typer.Context, ref: str, output: OutputOption = None) -> None:
    """Muestra una instancia EC2 (por id o tag Name)."""
    app_ctx: AppContext = ctx.obj
    instance = build_ec2_use_cases(app_ctx).get_instance.execute(ref)
    _render_instance(instance, ctx=app_ctx, output=output, title="instance show")


@instance_app.command("start")
def instance_start(
    ctx: typer.Context,
    ref: str,
    wait: Annotated[bool, typer.Option("--wait")] = False,
    output: OutputOption = None,
) -> None:
    """Inicia una instancia detenida."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    wiring = build_ec2_use_cases(app_ctx)
    instance = wiring.get_instance.execute(ref)
    use_case = wiring.start_instance
    if wait:
        instance = _run_with_spinner(
            app_ctx_out,
            f"Esperando a que '{instance.display_name}' esté RUNNING...",
            lambda: use_case.execute(instance, wait=True, timeout_s=_DEFAULT_TIMEOUT_S),
        )
    else:
        instance = use_case.execute(instance, wait=False, timeout_s=_DEFAULT_TIMEOUT_S)
    _render_instance(instance, ctx=app_ctx_out, title="instance start")


@instance_app.command("stop")
def instance_stop(
    ctx: typer.Context,
    ref: str,
    force: ForceOption = False,
    wait: Annotated[bool, typer.Option("--wait")] = False,
    yes: YesOption = False,
    output: OutputOption = None,
) -> None:
    """Detiene una instancia en ejecución. --force equivale a un apagón (riesgo de corrupción)."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    wiring = build_ec2_use_cases(app_ctx)
    instance = wiring.get_instance.execute(ref)
    confirm_destructive(
        app_ctx, action="detener", target=f"la instancia '{instance.display_name}'", assume_yes=yes
    )
    use_case = wiring.stop_instance
    if wait:
        instance = _run_with_spinner(
            app_ctx_out,
            f"Esperando a que '{instance.display_name}' esté STOPPED...",
            lambda: use_case.execute(
                instance, force=force, wait=True, timeout_s=_DEFAULT_TIMEOUT_S
            ),
        )
    else:
        instance = use_case.execute(instance, force=force, wait=False, timeout_s=_DEFAULT_TIMEOUT_S)
    _render_instance(instance, ctx=app_ctx_out, title="instance stop")


@instance_app.command("reboot")
def instance_reboot(ctx: typer.Context, ref: str, yes: YesOption = False) -> None:
    """Reinicia una instancia en ejecución."""
    app_ctx: AppContext = ctx.obj
    wiring = build_ec2_use_cases(app_ctx)
    instance = wiring.get_instance.execute(ref)
    confirm_destructive(
        app_ctx,
        action="reiniciar",
        target=f"la instancia '{instance.display_name}'",
        assume_yes=yes,
    )
    wiring.reboot_instance.execute(instance)
    app_ctx.console.print(f"[green]Reinicio de '{instance.display_name}' solicitado.[/]")


@instance_app.command("terminate")
def instance_terminate(
    ctx: typer.Context,
    ref: str,
    force: ForceOption = False,
    wait: Annotated[bool, typer.Option("--wait")] = False,
    yes: YesOption = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    output: OutputOption = None,
) -> None:
    """Termina una instancia. Se niega sin --force si no la gestiona esta CLI."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    wiring = build_ec2_use_cases(app_ctx)
    instance = wiring.get_instance.execute(ref)

    app_ctx.err_console.print(
        f"Instancia: {instance.display_name} ({instance.instance_id})  "
        f"Estado: {instance.state.value}  "
        f"Gestionada por esta CLI: {instance.managed_by_cli}"
    )
    if not dry_run:
        confirm_destructive(
            app_ctx,
            action="terminar",
            target=f"la instancia '{instance.display_name}'",
            assume_yes=yes,
        )

    use_case = wiring.terminate_instance

    def _terminate() -> Instance | None:
        return use_case.execute(
            instance, force=force, dry_run=dry_run, wait=wait, timeout_s=_DEFAULT_TIMEOUT_S
        )

    result = (
        _run_with_spinner(
            app_ctx_out, f"Esperando a que '{instance.display_name}' esté TERMINATED...", _terminate
        )
        if wait and not dry_run
        else _terminate()
    )

    if result is None:
        render(
            {
                "DryRun": True,
                "InstanceId": instance.instance_id,
                "Message": "La terminación habría tenido éxito.",
            },
            ctx=app_ctx_out,
            title="instance terminate",
        )
        return
    _render_instance(result, ctx=app_ctx_out, title="instance terminate")


@instance_app.command("console")
def instance_console(ctx: typer.Context, ref: str, output: OutputOption = None) -> None:
    """Muestra la salida de consola de una instancia (útil para depurar un arranque fallido)."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    wiring = build_ec2_use_cases(app_ctx)
    instance = wiring.get_instance.execute(ref)
    output_text = wiring.get_console_output.execute(instance.instance_id)
    render(
        {"InstanceId": instance.instance_id, "Output": output_text},
        ctx=app_ctx_out,
        title="instance console",
    )
