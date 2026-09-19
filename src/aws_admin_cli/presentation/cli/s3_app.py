"""S3 CLI commands: buckets, objects, policies, versioning, and presigned URLs.

Same shape as ``iam_app.py``: pull ``AppContext`` off ``ctx.obj``, build a
``Boto3S3Gateway`` and grab ``ctx.obj.resource_repository``, build a use case
from ``application.use_cases.s3``, build its request DTO from the parsed CLI
arguments, call ``.execute()``, and hand the result to ``render()``. No
business rule lives here.

Table vs. JSON output stays asymmetric (Fase 2's convention): JSON mode
returns full fields, table mode shows a curated column subset -- for objects
specifically, that means a human-readable size (``S3Object.human_size``) and
date in table mode, raw bytes in JSON mode.

Every upload/download shows a Rich progress bar on ``ctx.err_console``
(STDERR, never STDOUT), disabled automatically in JSON mode or when STDERR
isn't a TTY -- ``s3 cp ... --output json`` must produce clean, parseable JSON
on STDOUT no matter what.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn

from aws_admin_cli.application.dto.s3 import (
    CopyObjectRequest,
    CreateBucketRequest,
    DeleteBucketRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    DownloadObjectRequest,
    ListObjectsRequest,
    PresignUrlRequest,
    SetBucketPolicyRequest,
    SetBucketTagsRequest,
    SetVersioningRequest,
    UploadDirectoryRequest,
    UploadObjectRequest,
)
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.enums import OutputFormat
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.policy import PolicyDocument
from aws_admin_cli.domain.models.s3 import S3Uri
from aws_admin_cli.presentation.cli.callbacks import OutputOption, apply_output_override
from aws_admin_cli.presentation.cli.confirm import confirm_destructive
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.wiring import build_s3_use_cases

s3_app = typer.Typer(help="Gestión de S3.", no_args_is_help=True)
bucket_app = typer.Typer(help="Gestión de buckets S3.", no_args_is_help=True)
policy_app = typer.Typer(help="Gestión de la política de un bucket.", no_args_is_help=True)
s3_app.add_typer(bucket_app, name="bucket")
bucket_app.add_typer(policy_app, name="policy")


# -- Shared option types ------------------------------------------------------

ForceOption = Annotated[bool, typer.Option("--force", help="Vacía el bucket/prefijo primero.")]
YesOption = Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")]
TagOption = Annotated[list[str] | None, typer.Option("--tag", help="Tag CLAVE=VALOR (repetible).")]


def _s3_uri_argument(value: str) -> S3Uri:
    """Typer parser: convert an ``S3_URI`` argument, raising the domain ``ValidationError``.

    Uses Typer's ``parser=`` (not ``callback=``): a ``callback`` runs *after*
    Typer has already tried to build a click parameter type from the
    annotation itself, which fails for a non-builtin type like ``S3Uri`` --
    ``parser=`` is what actually converts the raw string.
    """
    return S3Uri.parse(value)


S3UriArgument = Annotated[S3Uri, typer.Argument(parser=_s3_uri_argument, metavar="S3_URI")]


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


def _render_model(
    model: BaseModel,
    *,
    ctx: AppContext,
    output: OutputFormat | None,
    table_fields: Sequence[str],
    title: str,
) -> None:
    """Render a single model: full fields as JSON (a bare object), a column subset as a table."""
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
    """Render several models: full fields as JSON (an array), a column subset as a table."""
    app_ctx = apply_output_override(ctx, output)
    dumped = [model.model_dump(by_alias=True, mode="json") for model in models]
    if app_ctx.output is OutputFormat.JSON:
        render(dumped, ctx=app_ctx, title=title)
        return
    rows: list[dict[str, Any]] = [
        {field: row[field] for field in table_fields if field in row} for row in dumped
    ]
    render(rows, ctx=app_ctx, title=title)


def _make_progress(app_ctx: AppContext) -> Progress:
    """Build a Rich progress bar for an upload/download -- always on STDERR.

    Disabled automatically in JSON output mode, or when STDERR isn't backed by
    a real terminal: ``s3 cp ... --output json > out.json`` must produce
    clean, parseable JSON on STDOUT regardless, and a piped/non-interactive
    run shouldn't scribble progress-bar frames into a log file either.
    """
    disable = app_ctx.output is OutputFormat.JSON or not app_ctx.err_console.is_terminal
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=app_ctx.err_console,
        disable=disable,
    )


_BUCKET_TABLE_FIELDS = ("Name", "CreationDate", "Region")
_OBJECT_TABLE_FIELDS = ("Key", "human_size", "LastModified", "StorageClass")


def _object_table_row(obj: Any) -> dict[str, Any]:
    dumped = obj.model_dump(by_alias=True, mode="json")
    dumped["human_size"] = obj.human_size
    return {field: dumped[field] for field in _OBJECT_TABLE_FIELDS if field in dumped}


def _render_object(obj: Any, *, ctx: AppContext, output: OutputFormat | None, title: str) -> None:
    """Render a single ``S3Object``: full fields as JSON, the human-readable-size table row.

    Not just ``_render_model`` with ``_OBJECT_TABLE_FIELDS``: ``human_size``
    is a computed property, absent from ``model_dump()``, so it needs
    ``_object_table_row``'s extra step to show up in table mode at all.
    """
    app_ctx = apply_output_override(ctx, output)
    if app_ctx.output is OutputFormat.JSON:
        render(obj.model_dump(by_alias=True, mode="json"), ctx=app_ctx, title=title)
        return
    render(_object_table_row(obj), ctx=app_ctx, title=title)


def _is_s3_uri(value: str) -> bool:
    return value.startswith("s3://")


# == Buckets ==================================================================


@bucket_app.command("create")
def bucket_create(
    ctx: typer.Context,
    name: str,
    region: Annotated[str | None, typer.Option("--region")] = None,
    enable_versioning: Annotated[bool, typer.Option("--enable-versioning")] = False,
    allow_public: Annotated[
        bool, typer.Option("--allow-public", help="NO aplica Block Public Access.")
    ] = False,
    if_not_exists: Annotated[
        bool, typer.Option("--if-not-exists", help="No falla si el bucket ya existe.")
    ] = False,
    output: OutputOption = None,
) -> None:
    """Crea un bucket de S3. Aplica Block Public Access salvo --allow-public."""
    app_ctx: AppContext = ctx.obj
    bucket = build_s3_use_cases(app_ctx).create_bucket.execute(
        CreateBucketRequest(
            name=name,
            region=region or app_ctx.settings.region,
            allow_public=allow_public,
            enable_versioning=enable_versioning,
            if_not_exists=if_not_exists,
        )
    )
    _render_model(
        bucket, ctx=app_ctx, output=output, table_fields=_BUCKET_TABLE_FIELDS, title="bucket create"
    )


@bucket_app.command("list")
def bucket_list(ctx: typer.Context, output: OutputOption = None) -> None:
    """Lista los buckets de S3 de esta cuenta."""
    app_ctx: AppContext = ctx.obj
    buckets = build_s3_use_cases(app_ctx).list_buckets.execute()
    _render_model_list(
        buckets, ctx=app_ctx, output=output, table_fields=_BUCKET_TABLE_FIELDS, title="bucket list"
    )


@bucket_app.command("info")
def bucket_info(ctx: typer.Context, name: str, output: OutputOption = None) -> None:
    """Muestra región, versionado, política, tags y nº de objetos de un bucket."""
    app_ctx: AppContext = ctx.obj
    info = build_s3_use_cases(app_ctx).get_bucket_info.execute(name)
    app_ctx_out = apply_output_override(app_ctx, output)
    payload = {
        "Name": info.name,
        "Region": info.region,
        "Versioning": info.versioning.status.value,
        "Policy": json.loads(info.policy.to_aws_json()) if info.policy else None,
        "Tags": info.tags,
        "ObjectCount": info.object_count,
    }
    render(payload, ctx=app_ctx_out, title="bucket info")


@bucket_app.command("delete")
def bucket_delete(
    ctx: typer.Context, name: str, force: ForceOption = False, yes: YesOption = False
) -> None:
    """Borra un bucket de S3."""
    app_ctx: AppContext = ctx.obj
    confirm_destructive(app_ctx, action="borrar", target=f"el bucket '{name}'", assume_yes=yes)
    build_s3_use_cases(app_ctx).delete_bucket.execute(DeleteBucketRequest(name=name, force=force))
    app_ctx.console.print(f"[green]Bucket '{name}' borrado.[/]")


@bucket_app.command("versioning")
def bucket_versioning(
    ctx: typer.Context,
    name: str,
    enable: Annotated[
        bool, typer.Option("--enable/--disable", help="Activa o suspende el versionado.")
    ],
) -> None:
    """Activa o suspende el versionado de un bucket."""
    app_ctx: AppContext = ctx.obj
    build_s3_use_cases(app_ctx).set_versioning.execute(
        SetVersioningRequest(name=name, enabled=enable)
    )
    state = "activado" if enable else "suspendido"
    app_ctx.console.print(f"[green]Versionado {state} en '{name}'.[/]")


@bucket_app.command("tags")
def bucket_tags(
    ctx: typer.Context, name: str, tag: TagOption = None, output: OutputOption = None
) -> None:
    """Muestra los tags de un bucket, o los reemplaza si pasas --tag."""
    app_ctx: AppContext = ctx.obj
    wiring = build_s3_use_cases(app_ctx)
    if tag:
        wiring.set_bucket_tags.execute(SetBucketTagsRequest(name=name, tags=_tags_to_dict(tag)))
    tags = wiring.get_bucket_tags.execute(name)
    app_ctx_out = apply_output_override(app_ctx, output)
    render(tags, ctx=app_ctx_out, title="bucket tags")


# == Bucket policy ============================================================


@policy_app.command("get")
def policy_get(ctx: typer.Context, name: str, output: OutputOption = None) -> None:
    """Muestra la política de un bucket."""
    app_ctx: AppContext = ctx.obj
    document = build_s3_use_cases(app_ctx).get_bucket_policy.execute(name)
    app_ctx_out = apply_output_override(app_ctx, output)
    if document is None:
        render({"Name": name, "Policy": None}, ctx=app_ctx_out, title="bucket policy get")
        return
    render(json.loads(document.to_aws_json()), ctx=app_ctx_out, title="bucket policy get")


@policy_app.command("set")
def policy_set(
    ctx: typer.Context,
    name: str,
    document_file: Annotated[Path | None, typer.Option("--document-file")] = None,
    document_json: Annotated[str | None, typer.Option("--document-json")] = None,
    allow_public: Annotated[
        bool, typer.Option("--allow-public", help='Permite Principal "*".')
    ] = False,
) -> None:
    """Reemplaza la política de un bucket."""
    app_ctx: AppContext = ctx.obj
    document = _resolve_policy_document(document_file, document_json)
    build_s3_use_cases(app_ctx).set_bucket_policy.execute(
        SetBucketPolicyRequest(name=name, document=document, allow_public=allow_public)
    )
    app_ctx.console.print(f"[green]Política aplicada a '{name}'.[/]")


@policy_app.command("delete")
def policy_delete(ctx: typer.Context, name: str, yes: YesOption = False) -> None:
    """Borra la política de un bucket."""
    app_ctx: AppContext = ctx.obj
    confirm_destructive(app_ctx, action="borrar la política de", target=f"'{name}'", assume_yes=yes)
    build_s3_use_cases(app_ctx).delete_bucket_policy.execute(name)
    app_ctx.console.print(f"[green]Política borrada de '{name}'.[/]")


# == Objects (top-level commands) =============================================


@s3_app.command("ls")
def s3_ls(
    ctx: typer.Context,
    uri: S3UriArgument,
    prefix: Annotated[str | None, typer.Option("--prefix")] = None,
    recursive: Annotated[
        bool, typer.Option("--recursive", help="No trates '/' como separador de prefijos.")
    ] = False,
    max_items: Annotated[int | None, typer.Option("--max-items")] = None,
    output: OutputOption = None,
) -> None:
    """Lista objetos bajo un S3 URI. Sin --recursive, agrupa por '/' (common prefixes)."""
    app_ctx: AppContext = ctx.obj
    effective_prefix = prefix if prefix is not None else uri.key
    delimiter = None if recursive else "/"
    listing = build_s3_use_cases(app_ctx).list_objects.execute(
        ListObjectsRequest(
            bucket=uri.bucket, prefix=effective_prefix, delimiter=delimiter, max_items=max_items
        )
    )
    app_ctx_out = apply_output_override(app_ctx, output)
    if app_ctx_out.output is OutputFormat.JSON:
        # A flat array, like every other `list` command's JSON output -- not the
        # ObjectListing wrapper object, which isn't directly iterable by a script
        # expecting "a list of entries" (`for row in json.loads(...)`).
        json_rows: list[dict[str, Any]] = [
            {"Key": p, "Type": "prefix"} for p in listing.common_prefixes
        ]
        json_rows.extend(o.model_dump(by_alias=True, mode="json") for o in listing.objects)
        render(json_rows, ctx=app_ctx_out, title="ls")
        return
    rows: list[dict[str, Any]] = [
        {"Key": p, "human_size": "-", "LastModified": "-", "StorageClass": "PREFIX"}
        for p in listing.common_prefixes
    ]
    rows.extend(_object_table_row(o) for o in listing.objects)
    render(rows, ctx=app_ctx_out, title="ls")


@s3_app.command("cp")
def s3_cp(
    ctx: typer.Context,
    source: str,
    dest: str,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
    content_type: Annotated[str | None, typer.Option("--content-type")] = None,
    storage_class: Annotated[str | None, typer.Option("--storage-class")] = None,
    metadata: Annotated[list[str] | None, typer.Option("--metadata")] = None,
    output: OutputOption = None,
) -> None:
    """Copia SOURCE a DEST. Local<->s3:// y s3://<->s3:// -- ambos locales no está soportado."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    wiring = build_s3_use_cases(app_ctx)
    source_is_s3 = _is_s3_uri(source)
    dest_is_s3 = _is_s3_uri(dest)

    if not source_is_s3 and not dest_is_s3:
        raise ValidationError(
            "SOURCE y DEST son ambos locales.",
            hint="Usa el comando `cp` de tu sistema operativo para copias locales.",
        )

    if source_is_s3 and dest_is_s3:
        src_uri = S3Uri.parse(source)
        dst_uri = S3Uri.parse(dest)
        if src_uri.key is None or dst_uri.key is None:
            raise ValidationError(
                "Una copia s3->s3 requiere una clave de objeto en ambos lados.",
                hint="Usa s3://bucket/clave en SOURCE y DEST.",
            )
        obj = wiring.copy_object.execute(
            CopyObjectRequest(
                src_bucket=src_uri.bucket,
                src_key=src_uri.key,
                dst_bucket=dst_uri.bucket,
                dst_key=dst_uri.key,
            )
        )
        _render_object(obj, ctx=app_ctx_out, output=None, title="cp")
        return

    if dest_is_s3:
        local_path = Path(source)
        dst_uri = S3Uri.parse(dest)

        if local_path.is_dir():
            summary = wiring.upload_directory.execute(
                UploadDirectoryRequest(
                    bucket=dst_uri.bucket,
                    prefix=dst_uri.key or "",
                    source_dir=local_path,
                    storage_class=storage_class,
                    overwrite=overwrite,
                )
            )
            render(
                {
                    "Uploaded": summary.uploaded,
                    "Skipped": summary.skipped,
                    "TotalBytes": summary.total_bytes,
                },
                ctx=app_ctx_out,
                title="cp",
            )
            return

        key = dst_uri.key or local_path.name
        request = UploadObjectRequest(
            bucket=dst_uri.bucket,
            key=key,
            source=local_path,
            content_type=content_type,
            metadata=_tags_to_dict(metadata),
            storage_class=storage_class,
            overwrite=overwrite,
        )
        total = local_path.stat().st_size if local_path.exists() else 0
        with _make_progress(app_ctx_out) as progress:
            task_id = progress.add_task(f"Subiendo {key}", total=total)
            obj = wiring.upload_object.execute(
                request, progress_callback=lambda n: progress.update(task_id, advance=n)
            )
        _render_object(obj, ctx=app_ctx_out, output=None, title="cp")
        return

    # s3 -> local
    src_uri = S3Uri.parse(source)
    if src_uri.key is None:
        raise ValidationError("Falta la clave de objeto en SOURCE.", hint="Usa s3://bucket/clave.")
    head = wiring.gateway.head_object(src_uri.bucket, src_uri.key)
    with _make_progress(app_ctx_out) as progress:
        task_id = progress.add_task(f"Descargando {src_uri.key}", total=head.size)
        final_path = wiring.download_object.execute(
            DownloadObjectRequest(
                bucket=src_uri.bucket, key=src_uri.key, destination=Path(dest), overwrite=overwrite
            ),
            progress_callback=lambda n: progress.update(task_id, advance=n),
        )
    render(
        {"Bucket": src_uri.bucket, "Key": src_uri.key, "Destination": str(final_path)},
        ctx=app_ctx_out,
        title="cp",
    )


@s3_app.command("rm")
def s3_rm(
    ctx: typer.Context,
    uri: S3UriArgument,
    recursive: Annotated[bool, typer.Option("--recursive")] = False,
    yes: YesOption = False,
) -> None:
    """Borra un objeto, o (con --recursive) todo lo que haya bajo el prefijo."""
    app_ctx: AppContext = ctx.obj
    wiring = build_s3_use_cases(app_ctx)

    if recursive:
        prefix = uri.key or ""
        listing = wiring.list_objects.execute(
            ListObjectsRequest(
                bucket=uri.bucket, prefix=prefix or None, delimiter=None, max_items=None
            )
        )
        confirm_destructive(
            app_ctx,
            action="borrar",
            target=f"{listing.key_count} objeto(s) bajo '{uri}'",
            assume_yes=yes,
        )
        count = wiring.delete_prefix.execute(DeletePrefixRequest(bucket=uri.bucket, prefix=prefix))
        app_ctx.console.print(f"[green]{count} objeto(s) borrado(s) de '{uri.bucket}'.[/]")
        return

    if uri.key is None:
        raise ValidationError(
            "Falta la clave de objeto.",
            hint="Usa s3://bucket/clave, o --recursive para borrar un prefijo entero.",
        )
    confirm_destructive(app_ctx, action="borrar", target=f"el objeto '{uri}'", assume_yes=yes)
    wiring.delete_object.execute(DeleteObjectRequest(bucket=uri.bucket, key=uri.key))
    app_ctx.console.print(f"[green]Objeto '{uri}' borrado.[/]")


@s3_app.command("presign")
def s3_presign(
    ctx: typer.Context,
    uri: S3UriArgument,
    expires_in: Annotated[
        int, typer.Option("--expires-in", help="Segundos de validez (1 - 604800).")
    ] = 3600,
    method: Annotated[str, typer.Option("--method", help="get | put")] = "get",
    output: OutputOption = None,
) -> None:
    """Genera una URL prefirmada para un objeto."""
    app_ctx: AppContext = ctx.obj
    if uri.key is None:
        raise ValidationError("Falta la clave de objeto.", hint="Usa s3://bucket/clave.")
    if method not in ("get", "put"):
        raise ValidationError(f"--method inválido: '{method}'.", hint="Usa get o put.")

    url = build_s3_use_cases(app_ctx).presign_url.execute(
        PresignUrlRequest(
            bucket=uri.bucket,
            key=uri.key,
            expires_in=expires_in,
            method="put" if method == "put" else "get",
        )
    )
    app_ctx_out = apply_output_override(app_ctx, output)
    render({"Url": url}, ctx=app_ctx_out, title="presign")
