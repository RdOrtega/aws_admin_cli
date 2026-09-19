# Referencia del manifiesto de stack

Un manifiesto de stack es un archivo YAML que declara, de forma determinista, un
conjunto de recursos de IAM/S3/EC2 y el orden en que deben crearse. `stack apply` los
aplica; `stack destroy` deshace únicamente lo que él mismo creó. Ver
[`examples/webapp-stack.yaml`](../examples/webapp-stack.yaml) para un ejemplo completo
y [`examples/README.md`](../examples/README.md) para la sintaxis de interpolación y las
dependencias implícitas.

## Forma general

```yaml
apiVersion: v1
name: mi-stack                 # ^[a-z][a-z0-9-]{1,40}$
description: Opcional.
resources:
  - id: algun-id                # ^[a-z][a-z0-9-]{1,62}$, único en el manifiesto
    kind: iam:role               # uno de los kinds de la tabla de abajo
    properties: { ... }          # específicas de cada kind, ver más abajo
    depends_on: [otro-id]        # opcional -- ver "Dependencias" en examples/README.md
```

- `apiVersion` es siempre `v1` (único valor soportado hoy).
- `name` identifica el stack en el estado persistido (`stack list`, `stack show NAME`,
  `stack destroy NAME`, ...) -- no tiene por qué coincidir con el nombre de archivo.
- Cada recurso necesita un `id` único dentro del manifiesto (no across manifiestos --
  dos stacks distintos pueden reutilizar el mismo `id` interno sin conflicto).
- **No existe, y nunca existirá, un `kind` de red** (`vpc:*`, `subnet:*`,
  `security-group:*`). Ver la sección "Por qué no hay recursos de red" al final.

## Catálogo de `kind`

| `kind` | Properties | Outputs |
|---|---|---|
| `iam:role` | `role_name` (str, requerida); uno de `service` (str, principal p. ej. `ec2.amazonaws.com`) o `trust_policy_file` (ruta a un JSON de trust policy); `description` (str, opcional); `max_session_duration` (int, opcional) | `arn`, `name` |
| `iam:policy` | `policy_name` (str, requerida); uno de `document` (mapa YAML inline con la forma de un documento de política IAM) o `document_file` (ruta a un JSON); `allow_wildcard` (bool, opcional, por defecto `false`) | `arn`, `name` |
| `iam:policy-attachment` | `policy_arn` (str, requerida -- normalmente `${otro-recurso.arn}`); `principal_type` (`user` \| `role`, requerida); `principal_name` (str, requerida) | ninguno pensado para referenciar desde otro recurso |
| `iam:instance-profile` | `role_name` (str, requerida); `profile_name` (str, opcional -- si se da, DEBE ser igual a `role_name`; ver nota abajo) | `arn`, `name` |
| `s3:bucket` | `bucket_name` (str, requerida); `region` (str, opcional -- por defecto la región activa del perfil); `enable_versioning` (bool, opcional); `allow_public` (bool, opcional) | `name`, `arn`, `domain_name` |
| `s3:bucket-policy` | `bucket_name` (str, requerida); uno de `document` o `document_file`; `allow_public` (bool, opcional) | ninguno |
| `ec2:key-pair` | `key_name` (str, requerida); `save_path` (str, opcional -- por defecto `~/.ssh`) | `key_name`, `fingerprint`, `path` (la ruta local del `.pem` -- NUNCA el material privado) |
| `ec2:instance` | `name`, `ami`, `instance_type`, `subnet`, `security_groups` (lista, requeridos); uno de `iam_role` (nombre de rol -- se garantiza/crea su instance profile) o `iam_instance_profile` (ARN de un instance profile ya existente); `key_name`, `user_data_file`, `volume_size` (int, GB), `public_ip` (bool), `confirm_public` (bool), `confirm_large` (bool), `count` (int), `tags` (mapa) -- todos opcionales | `instance_id`, `private_ip`, `public_ip` (si tiene), `az` |

Notas sobre casos concretos:

- **`iam:instance-profile` y la convención 1:1.** Este `kind` delega en el mismo caso
  de uso que `--iam-role` de `ec2 instance launch`
  (`ensure_instance_profile_for_role`), que usa el nombre del rol como nombre del
  instance profile. Si necesitas ese instance profile con un nombre distinto al del
  rol, este `kind` no lo soporta hoy -- usa `--iam-role` directamente en el `kind`
  `ec2:instance` en su lugar (que también garantiza el instance profile, con la misma
  convención).
- **`iam_role` vs `iam_instance_profile` en `ec2:instance`.** `iam_role` es la forma
  habitual: garantiza (crea si falta) el instance profile 1:1 de ese rol.
  `iam_instance_profile` adjunta un ARN de instance profile YA EXISTENTE directamente
  -- útil si ese profile lo declaraste como su propio recurso `iam:instance-profile`
  del mismo manifiesto (`iam_instance_profile: "${mi-profile.arn}"`) o si ya existía
  fuera del stack. Especificar ambos a la vez es un error de validación.
- **Guard rails de `ec2 instance launch` siguen aplicando.** `confirm_large` y
  `confirm_public` existen como properties precisamente para que esos guard rails
  (familia de instancia no permitida, IP pública) sigan siendo un opt-in explícito
  incluso en un manifiesto declarativo -- omitirlos dejando la instancia fuera de la
  allowlist, o pidiendo IP pública, hace fallar el `apply` con el mismo mensaje que
  la CLI de EC2.

## `created_by_stack`: qué se destruye y qué no

Cada recurso aplicado se marca `created_by_stack=true` si esta ejecución de `apply` lo
CREÓ de verdad, o `false` si un `kind` encontró que ya existía (por nombre) y lo
reutilizó. **Solo lo primero se toca en `stack destroy` o en un rollback.** Si tu
manifiesto declara `role_name: rol-compartido-de-otro-equipo` y ese rol ya existe,
`stack destroy` de ESE stack jamás lo borrará -- ni aunque el `apply` original lo haya
"reutilizado" en el sentido de adjuntarle una policy.

## Salida de `stack show` / `stack status`

Cada recurso reporta, además de sus `outputs`: `logical_id`, `kind`, `status`
(`pending` | `creating` | `created` | `skipped` | `failed` | `compensating` |
`compensated` | `compensation-failed` | `destroyed`), `physical_id`, `arn`,
`created_by_stack`, y `error` (solo si `status` es `failed` o alguna variante de
`compensation-failed`). `stack status NAME --refresh` además comprueba cada recurso
`created` contra AWS en tiempo real y lo marca `failed` con un `error` que empieza por
`"Drift:"` si ya no existe o cambió fuera de este stack.

## Por qué no hay recursos de red

Ningún `kind` de este catálogo crea una VPC, una subnet, o un security group -- y
nunca lo hará: el `ResourceKind` que los define no tiene, ni tendrá, un miembro
`vpc:*`/`subnet:*`/`security-group:*` (verificado por
`tests/unit/architecture/test_stack_no_network.py`, el mismo tipo de guardia
estructural que protege al módulo `vpc` de solo lectura). Un manifiesto que declare
uno de esos `kind` es rechazado por `stack validate`/`stack apply` con un mensaje que
explica la Separation of Duties -- exit code 64 -- en vez de un genérico "kind
desconocido". Un stack **referencia** la red existente por nombre
(`subnet: corp-private-1a`, `security_groups: [corp-web-sg]`), vía el mismo
`NetworkResolver` de solo lectura que usa `ec2 instance launch`. Ver la sección
"Separation of Duties" de [`docs/least-privilege.md`](least-privilege.md) para el
razonamiento completo, y `examples/network-stack.yaml` para un manifiesto que
demuestra el rechazo.
