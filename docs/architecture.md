# Architecture

`aws_admin_cli` follows a layered (clean/hexagonal-ish) architecture. Dependencies point
inward: presentation depends on application, application depends on domain, and
infrastructure implements domain ports. Domain never depends on anything outside itself.

```
┌─────────────────────────────────────────────────────────────┐
│  presentation/                                               │
│    cli/            Typer commands, argument parsing          │
│    cli/formatters/ Rich-based output rendering                │
└───────────────────────────┬───────────────────────────────────┘
                             │ calls
┌───────────────────────────▼───────────────────────────────────┐
│  application/                                                 │
│    use_cases/{iam,s3,vpc,ec2}/  orchestrated business flows    │
│    dto/                         data transfer objects          │
│    services/                    cross-use-case logic (resolvers)│
│    stacks/                      the Fase 6 orchestration engine │
│      steps/   one manifest-resource-kind adapter each           │
│      engine.py, registry.py                                     │
└───────────────────────────┬───────────────────────────────────┘
                             │ depends on (ports, not implementations)
┌───────────────────────────▼───────────────────────────────────┐
│  domain/                                                       │
│    models/    entities & value objects                          │
│    ports/     abstract interfaces implemented by infrastructure │
│    policies/  pure rule engines (no I/O)                        │
│    services/  pure cross-model logic (no I/O) -- dependency      │
│               graph, ${...} interpolation                       │
└───────────────────────────▲───────────────────────────────────┘
                             │ implements
┌───────────────────────────┴───────────────────────────────────┐
│  infrastructure/                                                │
│    aws/gateways/   boto3-backed port implementations             │
│    persistence/    local state & caching adapters                │
│    manifests/       stack YAML file I/O (yaml.safe_load only)     │
└─────────────────────────────────────────────────────────────────┘

  core/  cross-cutting: config, CLI context, exceptions, logging
         (used by every layer, depends on none of them)
```

This document was a stub for Fase 0 (scaffolding). Fase 1 filled in `core/` and
`infrastructure/aws/` (session/client construction, error mapping) plus the
`presentation/cli/formatters/` and the global CLI options. Fase 2 filled in
the local JSON resource ledger (`domain/ports/repository.py`,
`infrastructure/persistence/`) and the full IAM module. Fase 3 filled in the
full S3 module (buckets and objects) and, along the way, generalized
`ClientFactory` to support per-service `botocore.Config` overrides (see
below). Fase 4 filled in the full VPC module -- read-only by design (see
"El módulo VPC: read-only por diseño" below) -- and introduced two new kinds
of module: `domain/policies/` (pure rule engines) and `application/services/`
(cross-use-case application logic that isn't itself a use case). Fase 5 filled
in the full EC2 module (AMIs, key pairs, instance lifecycle) -- see "El flujo
de `ec2 instance launch`" below -- built to *consume* the VPC module's network
resolution and the IAM module's new instance-profile support, never to
duplicate either. Fase 6 filled in `stack` -- a declarative orchestration engine
(manifest -> plan -> apply/destroy, with saga-pattern rollback on failure; see
"El motor de stacks: patrón saga" below) built entirely on top of the use cases every
earlier phase already introduced: `application/stacks/steps/` never calls a gateway,
it adapts a manifest resource to whichever `iam`/`s3`/`ec2` use case already does the
real work.

## Flujo de una invocación

Every command goes through the same pipeline, regardless of which AWS service
it eventually touches:

```
 CLI flag (--profile, --endpoint-url, ...)
        │
        ▼
 Settings()                     -- env var / .env / class default already merged
        │  .with_overrides(...) -- only the flags the user actually passed
        ▼
 Settings (final, frozen)
        │
        ▼
 AppContext.build(settings)     -- Composition Root: wires everything, touches
        │                          no network, resolves no credentials
        ▼
 AppContext { settings, client_factory, console, err_console, logger }
        │
        ▼
 client_factory.sts() / .iam() / .s3() / .ec2()
        │  ClientFactory._client_kwargs() is the ONLY place that decides
        │  LocalStack vs real AWS: endpoint_url set → forwarded to boto3;
        │  endpoint_url None → boto3's normal AWS resolution, untouched.
        ▼
 boto3 client.<operation>(...)
        │
        ├── success ──────────────────────────────► render(data, ctx=app_ctx)
        │                                                   │
        │                                          ctx.output == TABLE → Rich table (stdout)
        │                                          ctx.output == JSON  → plain JSON (stdout)
        │                                          logs always go to STDERR, never STDOUT
        │
        └── botocore exception (ClientError / EndpointConnectionError / ...)
                    │
                    ▼
         aws_error_boundary(service, operation)   -- infrastructure/aws/error_mapper.py
                    │  maps the AWS error code (or connection failure) to a
                    │  domain exception (ResourceNotFoundError, AccessDeniedError,
                    │  ServiceUnavailableError, ...), with `hint` and `aws_code`
                    ▼
         AwsAdminCliError subclass propagates up through the command,
         through app(), uncaught by Click (it only catches its own
         ClickException/Exit)
                    │
                    ▼
         main.run()             -- the only place that turns a domain exception
                    │              into a process exit code
                    ├── prints "[bold red]Error:[/]" + message (+ hint) to STDERR
                    └── sys.exit(exc.exit_code)   -- e.g. 78 (config), 77 (access
                                                       denied), 69 (unavailable)
```

## Flujo de una operación IAM (`iam user create`, as an example)

Same pipeline as above, but shows how a command spans all four layers -- the
CLI never touches boto3, and the use case never touches boto3 or Typer:

```
 presentation/cli/iam_app.py :: user_create(ctx, name, tag, if_not_exists, ...)
        │  1. pulls AppContext off ctx.obj
        │  2. builds Boto3IamGateway(ctx.obj.client_factory)      -- infrastructure
        │  3. grabs ctx.obj.resource_repository                    -- infrastructure
        │  4. parses --tag KEY=VALUE into a dict (ValidationError if malformed)
        │  5. builds CreateUserRequest(name, path, tags, if_not_exists) -- application/dto
        ▼
 application/use_cases/iam/create_user.py :: CreateUserUseCase.execute(request)
        │  business rules live HERE, not in the CLI or the gateway:
        │    - if_not_exists + already exists → return the existing IamUser, no error
        │    - always sets the ManagedBy=aws-admin-cli tag
        │  calls only through ports (never boto3, never typer):
        ▼
 domain/ports/iam_gateway.py :: IamGateway.create_user(...)   -- Protocol, no implementation
        │  (application/domain know only this interface exists)
        ▼
 infrastructure/aws/gateways/boto3_iam_gateway.py :: Boto3IamGateway.create_user(...)
        │  with aws_error_boundary("iam", "CreateUser"):
        │      response = client.create_user(UserName=..., Path=..., Tags=...)
        │  IamUser.model_validate(response["User"])   -- PascalCase alias hydration
        ▼                                              -- domain/models/iam.py
 back up through the use case:
        │  repository.save(ResourceRecord(resource_type="iam:user", identifier=user.user_name, ...))
        ▼                                              -- domain/ports/repository.py
 infrastructure/persistence/json_repository.py :: JsonRepository.save(...)
        │  atomic write: temp file in the same directory, fsync, os.replace
        ▼                    -- ~/.aws_admin_cli/<profile>/resources.json
 back up to iam_app.py:
        └── render(user.model_dump(by_alias=True, mode="json"), ctx=app_ctx, ...)
                 table mode: curated columns (UserName, UserId, Arn, CreateDate)
                 json mode:  every field AWS returned, untouched
```

A `ClientError` raised anywhere inside `Boto3IamGateway` never reaches the use
case or the CLI as-is: `aws_error_boundary` translates it (e.g.
`EntityAlreadyExists` → `ResourceAlreadyExistsError`) before it leaves
`infrastructure/`, so `application/` and `presentation/` only ever see
`aws_admin_cli.core.exceptions` types -- exactly the same error-handling path
described above, just entered from a different command.

## `ClientFactory`'s per-service `botocore.Config` override registry

`ClientFactory._client_kwargs()` is the single place that decides `endpoint_url`
(LocalStack vs real AWS, described above). Fase 3 needed a *second*, narrower kind of
per-service knob: when `endpoint_url` is set, an S3 client must also be told to use
**path-style addressing** (`https://<endpoint>/<bucket>/<key>`) instead of boto3's
default virtual-hosted-style (`https://<bucket>.<endpoint>/<key>`) -- LocalStack doesn't
own a wildcard DNS entry for `<bucket>.localhost`, so a virtual-hosted request never
resolves. No other AWS service needs anything like this today, but the *shape* of "some
services need extra `botocore.Config` fields under some conditions" is generic, so it's
solved once, generically, rather than as an S3 special case:

```python
_SERVICE_CONFIG_OVERRIDES: Final[dict[str, Callable[[Settings], dict[str, Any]]]] = {
    "s3": _s3_config_override,
}
```

`_botocore_config(service_name)` builds the base `Config` kwargs, then looks up
`service_name` in the registry and merges in whatever that override function returns
(here, `{"s3": {"addressing_style": "path"}}` only when `settings.endpoint_url` is set --
real AWS never needs it). Adding a similar quirk for another service later (e.g. a
different signature version, or a retry override) means writing one more
`Callable[[Settings], dict[str, Any]]` and registering it -- **not** adding another
`if service_name == "..."` branch inside `_botocore_config`. That's deliberate: a growing
chain of `if service_name ==` checks is exactly the kind of thing the Open/Closed
Principle warns against (every new service quirk would mean re-editing and re-testing
the same function), and it's enforced by a standing grep in the verification checklist
(`grep -rn "== \"s3\"\|== 's3'" client_factory.py` must always be empty) rather than left
as a style preference.

## Where `application/services/` fits, and why the resolver isn't a use case

Every other thing under `application/` so far has been either a `dto/` (a plain data
carrier) or a `use_cases/{module}/` entry (one file, one verb, called directly from the
CLI). `NetworkResolver` (`application/services/network_resolver.py`, introduced in Fase
4) is neither: it doesn't do one user-facing action, it's a *building block* several
different use cases and CLI commands share -- `vpc resolve` and every `vpc
subnet`/`vpc sg` command that accepts a human reference instead of a raw ID; and, since
Fase 5, EC2's own `ec2 instance launch --subnet corp-private-1a --sg corp-web-sg`, which
needs to turn those names into `subnet-...`/`sg-...` IDs the same way, and neither
reimplements that logic nor imports a `vpc` use case just to borrow it.

That's the dividing line `application/services/` exists to draw: a `use_cases/{module}/`
file is *the* thing a single CLI command does end to end; a `services/` file is logic
more than one of those things needs, factored out so it has exactly one implementation
(and, concretely for `NetworkResolver`, one cache -- resolving three subnet references
costs one `describe_subnets` call, not three, because the cache lives in one place all
three lookups share). Still pure application layer: `NetworkResolver` depends only on
the `VpcGateway` Protocol, same as any use case -- no boto3, no typer.

`AmiResolver` (`application/services/ami_resolver.py`, Fase 5) is the same kind of
building block, one layer over: it turns `--ami` (an `ami-...` id, a known distro alias,
or an exact name) into a resolved `Ami`, cached per instance, depending only on the
`Ec2Gateway` Protocol -- the same shape as `NetworkResolver`, just for AMIs instead of
network resources.

## El flujo de `ec2 instance launch`

The most important use case in this codebase composes every resolver and every
cross-module safeguard introduced so far -- read-only network resolution (Fase 4), the
new IAM instance-profile use case (Fase 5), and EC2's own launch guard rails, in that
order, before a single AWS mutation happens:

```
 presentation/cli/ec2_app.py :: instance_launch(ctx, name, --ami, --subnet, --sg, ...)
        │  resolves AMI/subnet/SGs FIRST, so the confirmation prompt below shows
        │  what was actually resolved -- never the raw string the user typed
        ▼
 application/services/ami_resolver.py :: AmiResolver.resolve(--ami)          -- read-only
 application/services/network_resolver.py :: NetworkResolver.resolve_subnet/  -- read-only
                                              resolve_security_groups(--subnet, --sg)
        │
        ▼
 err_console (STDERR): "Se va a lanzar: AMI ami-xxx (...), tipo t3.micro (2 vCPU, 1 GiB,
                         familia t3), subnet subnet-xxx (corp-private-1a, AZ us-east-1a),
                         security groups sg-xxx (corp-web-sg), IAM role demo-ec2-role"
        │
        ▼
 confirm_destructive(...)               -- skipped only with --yes or --dry-run
        │
        ▼
 application/use_cases/ec2/launch_instance.py :: LaunchInstanceUseCase.execute(request)
        │
        │  1. ami_resolver.resolve(ami_ref)                          -- read-only
        │  2. network_resolver.resolve_subnet/resolve_security_groups -- read-only
        │  3. IF --iam-role: ensure_instance_profile_for_role.py       -- Fase 5's IAM
        │       .execute(role_name) -> instance profile ARN              extension:
        │       (creates the instance profile + attaches the role,       creates what's
        │        idempotently, if either is missing; NEVER creates       missing, never
        │        the role itself -- that stays `iam role create`)        the role itself
        │  4. domain.models.ec2.validate_user_data(user_data)          -- size + secret-
        │       raises ValidationError before anything is created        pattern guard rail
        │  5. domain.policies.launch_rules.check_instance_type/          -- pure, zero I/O
        │       check_public_ip                                          gates
        │  6. builds LaunchSpec with mandatory tags (ManagedBy=aws-admin-cli,
        │       CreatedAt=<iso>, Name=<name>) -- application/dto -> domain/models/ec2.py
        │  7. _compute_client_token(spec, nonce) -- sha256 of the launch parameters
        │       (mandatory tags' CreatedAt excluded -- see the function's docstring)
        │       plus --client-token if given: the SAME command run twice produces the
        │       SAME token, so AWS's own ClientToken dedup absorbs the retry
        ▼
 domain/ports/ec2_gateway.py :: Ec2Gateway.run_instance(spec, client_token, dry_run)
        │                                                          -- Protocol
        ▼
 infrastructure/aws/gateways/boto3_ec2_gateway.py :: Boto3Ec2Gateway.run_instance(...)
        │  with aws_error_boundary("ec2", "RunInstances"):
        │      client.run_instances(
        │          ClientToken=..., DryRun=...,
        │          NetworkInterfaces=[_build_network_interface(spec)],  -- subnet + SGs +
        │                                                                  AssociatePublicIp
        │                                                                  MUST travel here,
        │                                                                  never top-level
        │          MetadataOptions={"HttpTokens": "required", ...},    -- IMDSv2 mandatory
        │          BlockDeviceMappings=[{..., "Encrypted": True,
        │                                     "DeleteOnTermination": True}],
        │          TagSpecifications=[...],                            -- tags AT launch,
        │      )                                                          never a follow-up
        │                                                                  CreateTags call
        │  DryRunOperation ClientError -> translated to a SUCCESS result, never an exception
        ▼                                                          -- domain/models/ec2.py
 back up through the use case:
        │  IF request.wait: gateway.wait_for_state([id], RUNNING, timeout_s, poll_s)
        │       -- boto3 waiter; WaiterError -> OperationTimeoutError (exit code 75)
        │  IF NOT dry_run: repository.save(ResourceRecord(resource_type="ec2:instance",
        │       metadata={ami, instance_type, subnet_id, security_group_ids,
        │                 iam_instance_profile_arn, name}))
        ▼
 back up to ec2_app.py:
        └── render(instance.model_dump(by_alias=True, mode="json"), ctx=app_ctx, ...)
```

Two things worth calling out that aren't obvious from the diagram:

- **Step 3 can create AWS resources (the instance profile) before step 6-8's
  actual launch is attempted.** If validation or the launch itself fails after
  that, `LaunchInstanceUseCase.execute` does NOT roll the instance profile
  back (that's a Fase 6 concern, not this one) -- it logs a WARNING naming
  what was already created, so the operator isn't left guessing what exists.
- **Every resolver call in steps 1-2 is read-only**, and stays that way
  structurally, not just by convention: `Ec2Gateway`'s own methods are
  checked by the same `tests/unit/architecture/test_vpc_readonly.py` that
  guards the `vpc` module (see `docs/least-privilege.md`'s "Separation of
  Duties" section) -- if `--sg` names a security group that doesn't exist,
  `resolve_security_groups` raises `ResourceNotFoundError` and nothing after
  step 2 ever runs. No security group, subnet, or VPC is ever created to make
  a launch succeed.

## El motor de stacks: patrón saga

Fase 6 necesitaba una garantía que ninguna fase anterior había necesitado: aplicar
*varios* recursos relacionados como una sola operación lógica, de forma que un fallo a
mitad de camino no deje media infraestructura huérfana. AWS no ofrece transacciones
multi-servicio (no hay forma de envolver `CreateBucket` + `CreateRole` +
`AttachRolePolicy` + `RunInstances` en un `COMMIT`/`ROLLBACK` atómico) -- así que
`StackEngine` implementa el **patrón saga**: cada paso que tiene éxito registra cómo
deshacerse a sí mismo (`StackStep.compensate`), y si un paso posterior falla, el motor
invoca esas compensaciones en orden inverso, una por una.

### Por qué compensación y no transacciones

Una transacción de base de datos deshace escribiendo por encima de un WAL antes de
que nadie más vea el cambio a medias. Eso no existe aquí: en el momento en que
`iam:role` tiene éxito, ese rol YA es visible -- para cualquier otra llamada a la API,
para la consola de AWS, para otro proceso corriendo en la misma cuenta. No hay forma
de "deshacer sin que se haya notado". Compensar es la única opción: en vez de
deshacer una escritura no confirmada, se ejecuta una segunda operación (normalmente
un `Delete*`) que revierte el efecto observable de la primera. Es exactamente el
patrón saga descrito por Garcia-Molina/Salem (1987) para transacciones de larga
duración que cruzan múltiples sistemas -- cada paso es su propia transacción local,
y la atomicidad del conjunto la da la cadena de compensaciones, no un `COMMIT` único.

### Por qué solo se compensa lo que el propio stack creó

Esta es la regla más importante de todo el motor, y vive en un solo campo:
`ResourceState.created_by_stack`. Un `StackStep.execute()` que encuentra un recurso ya
existente (por nombre) y simplemente lo reutiliza reporta `created_by_stack=False`; el
motor copia ese valor tal cual al estado persistido y **jamás** lo recalcula ni lo
cuestiona. `StackEngine._rollback` y `StackEngine.destroy` filtran explícitamente por
`created_by_stack=True` antes de siquiera considerar compensar un recurso.

La razón no es solo defensiva: un manifiesto que referencia
`role_name: rol-compartido-de-otro-equipo` porque ese rol legítimamente ya existe
NUNCA debe verse borrado porque un `apply` posterior de ESE stack falló en otro paso,
ni porque alguien ejecutó `stack destroy`. Sin esta distinción, "declarar" un recurso
existente en un manifiesto sería indistinguible de "adoptarlo para destruirlo
eventualmente" -- una trampa que un operador pagaría muy caro la primera vez que
pisara.

### Qué pasa cuando la compensación falla

Una compensación puede fallar por las mismas razones que cualquier llamada a AWS
(permisos, throttling, el recurso ya no existe de una forma que el `Delete*`
correspondiente no tolera). `StackEngine._rollback` envuelve CADA compensación
individualmente: si una lanza una excepción, se registra como
`COMPENSATION_FAILED` (con el error) y el bucle **continúa** con el resto -- nunca
aborta el rollback completo por un solo fallo de limpieza. Al final:

- Si todas las compensaciones tuvieron éxito, el stack queda `ROLLED_BACK` y
  `StackApplyError.orphaned` está vacío.
- Si al menos una falló, el stack queda `ROLLBACK_INCOMPLETE` y `.orphaned` lista
  cada `ResourceState` que sigue vivo en AWS, con su `physical_id` -- lo que un
  operador necesita para limpiarlo a mano o para diagnosticar por qué falló la
  compensación antes de reintentar.

En ambos casos, `StackApplyError.__cause__` es SIEMPRE el error original que disparó
el rollback -- nunca el error de una compensación fallida. Perder de vista *por qué*
falló el `apply` original detrás de un error de limpieza secundario sería el fallo de
diagnóstico más caro que este motor podría cometer; `application/stacks/engine.py`
construye la excepción con `raise StackApplyError(...) from failure` exactamente para
que eso no sea posible aunque una compensación también lance.

### Persistencia incremental: la otra mitad de la garantía

Compensar automáticamente resuelve "un paso falló, deshaz lo anterior" -- pero no
resuelve "el proceso murió a mitad del `apply` (o del rollback), antes de que la
compensación automática pudiera siquiera intentarse". Por eso
`StackEngine.apply`/`_rollback`/`destroy` persisten el `StackState` completo
(vía `Repository[StackState]`, el mismo `JsonRepository` atómico -- escritura a un
fichero temporal + `fsync` + `os.replace` -- que ya usa el ledger de recursos)
INMEDIATAMENTE después de cada paso, nunca solo al final. Ver la última sección de
este documento (o la pregunta 7 del informe de verificación de Fase 6) para el
escenario completo de recuperación tras un crash.

### Por qué los steps delegan y el motor nunca conoce un `if kind == ...`

`application/stacks/steps/base.py` define el contrato (`execute`/`compensate`/
`still_exists`) que CUALQUIER `StackStep` debe cumplir. Cada implementación concreta
(`IamRoleStep`, `S3BucketStep`, `Ec2InstanceStep`, ...) es un adaptador puro: traduce
`properties` ya interpoladas en la request de un caso de uso YA EXISTENTE, lo invoca,
y traduce el resultado a `StepResult` -- nunca llama a un gateway directamente, nunca
reimplementa una validación que ese caso de uso ya posee (verificado por
`grep -rn "client_factory\|gateway\." application/stacks/steps/`, que debe salir
vacío). `application/stacks/registry.py` es el único lugar que sabe qué `StackStep`
corresponde a cada `ResourceKind` -- añadir un tipo de recurso nuevo significa escribir
una clase y una entrada en ese diccionario; `StackEngine` no se toca nunca, el mismo
espíritu Open/Closed que `ClientFactory._SERVICE_CONFIG_OVERRIDES` (ver más arriba).

## El módulo VPC: read-only por diseño

Fase 4 introduced the first module in this codebase with a hard architectural
constraint baked in from the port down: `VpcGateway` (`domain/ports/vpc_gateway.py`)
exposes *only* `describe_*` methods. There is no `create_vpc`, no
`authorize_security_group_ingress`, not even a private mutating helper inside
`Boto3VpcGateway` -- see `docs/least-privilege.md`'s "Separation of Duties" section for
the reasoning (the base network is Networking/SecOps's resource, shared by everything
that runs in the account, so it isn't administered by the same tool that administers
per-workload resources like IAM users or S3 buckets).

What makes this a guarantee rather than a convention is
`tests/unit/architecture/test_vpc_readonly.py`: it inspects `VpcGateway` and
`Boto3VpcGateway` via `inspect`, asserts every public method matches `^describe_`, and
greps `boto3_vpc_gateway.py` and `presentation/cli/vpc_app.py` for mutating-verb
prefixes (`create_`, `delete_`, `modify_`, `authorize_`, `revoke_`, `associate_`,
`attach_`, `replace_`, `update_`, `put_`, ...). A pull request that adds a mutation
anywhere in this module fails that test before it fails a human reviewer.

One consequence worth calling out explicitly: `domain/policies/sg_audit_rules.py` (the
security-group audit engine) is pure domain -- it takes a `SecurityGroup` and returns
`SecurityFinding`s, never touches AWS, never fixes anything it finds. Every
`SecurityFinding.recommendation` is phrased as a request to the team that owns the
resource ("Solicitar a Networking/SecOps..."), not as an instruction the tool could
execute itself -- `vpc sg audit` reports, it never remediates, for the same
Separation-of-Duties reason the port has no write methods.

Fase 6's stack manifests inherit this rule rather than repeating it:
`domain/models/stack.py`'s `ResourceKind` simply has no `vpc:`/`subnet:`/
`security-group:` member, and `ResourceSpec`'s own field validator rejects a `kind`
starting with any of those prefixes at parse time -- before `stack apply` ever gets
near a gateway -- with a message that names Separation of Duties explicitly rather than
a generic "unknown kind" error.
`tests/unit/architecture/test_stack_no_network.py` is the standing guarantee, same
spirit as `test_vpc_readonly.py` above: it asserts no `ResourceKind` member starts with
a network prefix, and greps every file under `application/stacks/` for the same
mutating-call substrings (`create_security_group`, `authorize_security_group`,
`create_subnet`, `create_vpc`, `modify_subnet`, `revoke_security_group`). A stack
*consumes* the network -- `ec2:instance`'s `subnet`/`security_groups` properties
resolve through the exact same read-only `NetworkResolver` `ec2 instance launch` uses
-- it never creates or modifies it.

## `presentation/wiring.py`: one composition root, two presentation layers

Before the interactive TUI existed, every Typer command in `presentation/cli/*.py`
built its own use case(s) inline (`CreateUserUseCase(gateway=Boto3IamGateway(...), ...)`).
That was fine with exactly one caller per use case. It stops being fine the moment a
second caller needs the *same* object graph: two independent construction sites for one
use case are two places that can quietly drift apart (a new required constructor
argument added to one, forgotten in the other; a repository wired from a different
source). `presentation/wiring.py` is the fix: `build_iam_use_cases(ctx)` /
`build_s3_use_cases(ctx)` / `build_vpc_use_cases(ctx)` / `build_ec2_use_cases(ctx)` /
`build_stack_engine(ctx)` are the *only* places `Boto3IamGateway`/`Boto3S3Gateway`/
`Boto3VpcGateway`/`Boto3Ec2Gateway` and everything built from one are ever constructed.
`presentation/cli/*.py` and `presentation/tui/` both call these instead of building
anything themselves -- see each `build_*_use_cases` function's own docstring for why
this is still "lazy" (no boto3 client is ever created just from calling it) even though
every use case inside its returned dataclass is built eagerly.

## The interactive TUI: `Prompter` as a port, guard rails as a pattern, not a rule to copy

A bare `aws-admin-cli` invocation (or `--interactive`/`-i`) launches an arrow-key
menu instead of printing help -- see the README's "Modo interactivo" section for the
user-facing behavior. Architecturally, `presentation/tui/` is a sibling of
`presentation/cli/`, not a layer on top of it: both are built on
`presentation/wiring.py`, and `presentation/tui/` never imports `typer`/`click`, and
`presentation/cli/` never imports anything from `presentation/tui/`.

**`Prompter` is a port**, in exactly the same sense `IamGateway`/`S3Gateway` are:
`presentation/tui/prompter.py` declares it as a `Protocol` (`select`/`checkbox`/`text`/
`confirm`/`path`/`pause`), `QuestionaryPrompter` is its one real adapter (backed by
`questionary`, the only new runtime dependency this feature added), and
`tests/fakes/prompter.py`'s `FakePrompter` (a scripted response queue, never a
`Mock()`) is its test double -- same three-part shape as every gateway in this
codebase. The port exists because a `Flow` must never know *how* it's being asked a
question, only that it will get an answer (or `None`/`False` for "cancelled") --
this is what lets every flow test run without a terminal at all.

**One cancellation contract, everywhere.** Every `Prompter` method returns `None`
(`False` for `confirm`) the instant the user cancels (Ctrl+C, Esc, or EOF from a
closed stdin) -- it never lets `KeyboardInterrupt`/`EOFError` escape. A `Flow` never
wraps a `Prompter` call in `try/except` for either of those; it only ever checks the
return value. This is what lets `NavigationStack` (`presentation/tui/navigation.py`)
be a single explicit `while` loop over a plain list of flows -- never recursion between
menus -- with navigation depth bounded by the stack's own length, not by the Python
call stack, no matter how many times a user drills in and backs out.

**Guard rails are never replicated in the TUI, only reacted to.** Every validation
rule this tool enforces (`--force`, `--allow-wildcard`, `--confirm-large`, "this
resource isn't managed by this CLI", ...) lives exactly once, in `application`/
`domain`, and raises `ValidationError`. A CLI flag and a TUI flow are two different
*ways to answer* the question a guard rail already asked, never two independent
*places that ask it*. Concretely: `S3Flow._delete_bucket` calls
`DeleteBucketUseCase.execute(..., force=False)` first, exactly like `s3 bucket delete`
does without `--force` -- it never calls `list_objects` first to check whether the
bucket looks empty. Only when the use case itself raises `ValidationError` does the
flow offer to retry with `force=True`. This "try plain, catch, offer the escape hatch"
shape repeats in `IamFlow` (user/role delete vs. attached policies) and `Ec2Flow`
(instance terminate vs. the `ManagedBy=aws-admin-cli` tag) -- see
`tests/integration/presentation/test_s3_flow.py` for the two-call proof (the plain
call fails, the forced retry succeeds, and declining the retry leaves the resource
untouched).

**The flow registry is Open/Closed, same spirit as the stack step registry.** Adding a
sixth top-level TUI screen means: write one module under `presentation/tui/flows/`
implementing `Flow` (`title: ClassVar[str]`, `__init__(ctx, prompter)`,
`menu() -> NavAction`), and add it to `FLOWS` in `flows/registry.py` --
`presentation/tui/app.py`'s main menu never grows an `if kind == ...` branch; it just
reads every registered flow's `title` (a `ClassVar`, read off the class itself, before
any flow is ever instantiated) to build its own selection list.

**VPC stays read-only in the TUI too.** `VpcFlow` only ever calls the same `describe_*`
use cases `vpc_app.py` does, and includes one *disabled* "Cambios de red" entry whose
selection is unreachable -- it exists purely so a user looking for a create/modify
option finds an explanation (Networking owns that) instead of a dead end. This is
enforced the same way `test_vpc_readonly.py` enforces it for the CLI module: a
mutating-verb regex grep against `presentation/tui/flows/vpc_flow.py`'s source.

## The simulated LocalStack network: `corp-main-vpc`

Every `vpc`/`ec2`/TUI command that needs a real network to point at (rather than mocked
IAM/S3 calls, which moto handles in-process) runs against a small, fixed network that
`localstack/init/01-bootstrap.sh` seeds. As that script's own header comment says: it is
**not** part of `aws_admin_cli` -- it's a local double for what Networking/SecOps would
already have delivered in a real account, played back once (LocalStack runs every file
under `localstack/init/ready.d/` on first boot) or on demand via `make seed`. Running it
never touches the `vpc` module's own code path -- the CLI only ever *reads* what this
script created, via `vpc list`/`vpc sg audit`/etc. -- so seeding this network is not a
loophole in the read-only guarantee described above; see
`docs/least-privilege.md`'s "Separation of Duties" section for the same point made in
more detail.

The script is idempotent (every resource is looked up by its `Name`/`GroupName` tag
before being created, and `make seed` can be re-run any number of times against an
already-seeded LocalStack without duplicating or erroring), which is why the same
network survives across `docker compose up -d`/`down` cycles as long as the `./volume`
bind mount (`gresau/localstack-persist`'s on-disk state -- LocalStack's own
`PERSISTENCE=1` is Pro-only and a no-op on the community image) isn't wiped with
`docker compose down -v`.

### Topology

```
corp-main-vpc (10.0.0.0/16)
  |
  +-- corp-main-igw  (attached)
  |
  +-- corp-public-rtb  (0.0.0.0/0 -> corp-main-igw)
  |     |
  |     +-- corp-public-1a   10.0.1.0/24   us-east-1a   map-public-ip-on-launch=true
  |     +-- corp-public-1b   10.0.2.0/24   us-east-1b   map-public-ip-on-launch=true
  |
  +-- (no route table association -- no route to the IGW)
        |
        +-- corp-private-1a  10.0.11.0/24  us-east-1a   map-public-ip-on-launch=false
        +-- corp-private-1b  10.0.12.0/24  us-east-1b   map-public-ip-on-launch=false
```

A subnet's `is_public`/`Visibility` (as `vpc subnet list` and `NetworkResolver` compute
it) isn't a naming convention here -- it's genuinely derived from whether the subnet's
route table has a `0.0.0.0/0` route to an internet gateway, exactly like real AWS. That's
deliberate: it lets `demo-vpc`'s `vpc subnet list --public`/`--private` and the "Public
IP into a private subnet" EC2 guard rail (see "El flujo de `ec2 instance launch`" above)
exercise the real classification logic against real describe-calls, not a hardcoded tag.

### Security groups: deliberately imperfect, on purpose

Three of the five seeded security groups have a real, findable misconfiguration --
this is intentional, not an oversight, so `vpc sg audit`'s rule engine
(`domain/policies/sg_audit_rules.py`) and its TUI/CLI presentation always have
something genuine to report against a fresh environment, in both a passing demo
(`make demo-vpc`) and this project's own e2e tests:

| Security group | Ingress rule | Audit finding |
|---|---|---|
| `corp-web-sg` | tcp/80, tcp/443 from `0.0.0.0/0` | none -- this is what a public web tier is supposed to look like |
| `corp-bastion-sg` | tcp/22 from `0.0.0.0/0` | **CRITICAL** -- SSH open to the world |
| `corp-db-sg` | tcp/5432 from `0.0.0.0/0` | **CRITICAL** -- PostgreSQL open to the world |
| `corp-internal-sg` | all protocols from `10.0.0.0/16` only | none -- broad, but scoped to the VPC's own CIDR, never `0.0.0.0/0` |
| `corp-legacy-sg` | tcp/1024-65535 from `0.0.0.0/0` | **HIGH** -- a wide ephemeral-port range open to the world |

`corp-web-sg`, `corp-private-1a`, and an instance profile for the IAM role
`ec2 instance launch --iam-role` names (created on demand -- never by this script) are
the actual resources `demo-ec2`/`demo-stack` launch an instance against
(`--subnet corp-private-1a --sg corp-web-sg`); the other three security groups exist
purely as audit fixtures and are never referenced by a launch.

## Running the e2e test suite

This project's tests come in three tiers, matched to what they need to be true to run at
all -- `pyproject.toml`'s `[tool.pytest.ini_options]` markers name exactly this split:

| Marker | What it hits | Speed | Runs by default? |
|---|---|---|---|
| (none / `unit`) | Nothing external -- fakes/in-memory doubles only (`tests/fakes/`) | fastest | yes |
| `integration` | `moto`'s in-process botocore interception -- no real network, no Docker | fast | yes |
| `e2e` | A real, running LocalStack **container**, over real HTTP to `localhost:4566` | slowest | **no** -- opt-in only |

`moto` (used by `integration`) and LocalStack (used by `e2e`) are not redundant with each
other: moto patches botocore in the same process, which is fast but can't exercise
anything Docker-networking-shaped (LocalStack's own container health, port binding,
state surviving a restart via `gresau/localstack-persist`) or catch a divergence between
moto's emulation and LocalStack's. `e2e` is the tier that proves the CLI works against
something that behaves
like a real AWS endpoint end to end, including the network topology above; `integration`
is the tier that runs on every commit because it doesn't need Docker at all.

### Prerequisites

```bash
make up      # docker compose up -d -- starts the LocalStack container
make seed    # re-runs localstack/init/01-bootstrap.sh -- idempotent, safe to repeat
```

`tests/e2e/conftest.py`'s `skip_if_localstack_down` fixture checks
`socket.create_connection(("localhost", 4566))` before each e2e test and skips (not
fails) it if nothing answers -- so forgetting `make up` produces a clean "skipped"
instead of a wall of connection-refused errors. `test_vpc_localstack.py` adds its own
`skip_if_bootstrap_not_seeded` on top, for the tests that specifically need
`corp-main-vpc` to exist -- so forgetting `make seed` skips just as cleanly. The
conftest's `_fake_aws_profile` fixture (autouse) points boto3 at a throwaway,
session-local profile with LocalStack's universally-accepted dummy static
credentials -- no real AWS credentials are ever needed to run this tier.

### Running it

```bash
make test-e2e
# equivalent to:
poetry run pytest -m e2e --no-cov
```

`--no-cov` is not optional here: `pyproject.toml`'s coverage gate (`fail_under = 80`) is
a whole-repo threshold, and an e2e-only subset touches a small fraction of the codebase
by design -- running it with coverage enabled would fail the gate for a reason that has
nothing to do with whether the e2e tests themselves passed. `make test`/`make cov`/
`make check` (the ones that DO enforce the coverage gate) all pass `-m "not e2e"` for the
same reason in reverse: the e2e tier's slowness and Docker dependency make it unsuitable
as part of the fast, always-on suite those targets run.

To run the full suite, including e2e, in one pass:

```bash
poetry run pytest -m "not e2e" && poetry run pytest -m e2e --no-cov
```

(two invocations, not one `pytest` with no `-m` filter, because the coverage gate only
makes sense against the first command's selection -- see above.)
