# Ejemplos de manifiesto de stack

Ver también [`docs/stack-manifest.md`](../docs/stack-manifest.md) para la referencia
completa del formato (todos los `kind`, sus properties y sus outputs).

## `webapp-stack.yaml`

Un stack de ejemplo: un bucket S3, un rol de IAM, una política de solo-lectura sobre
ese bucket, su adjunto al rol, y una instancia EC2 que lo asume -- todo sobre la red
sembrada por `localstack/init/01-bootstrap.sh` (`corp-private-1a`, `corp-web-sg`).

```bash
poetry run aws-admin-cli stack validate examples/webapp-stack.yaml
poetry run aws-admin-cli stack plan examples/webapp-stack.yaml
poetry run aws-admin-cli stack apply examples/webapp-stack.yaml --yes
poetry run aws-admin-cli stack destroy demo-webapp --yes
```

## Sintaxis de interpolación

Una property puede referenciar el output de OTRO recurso del mismo manifiesto con
`${<id-lógico>.<output-key>}`:

```yaml
resources:
  - id: app-bucket
    kind: s3:bucket
    properties:
      bucket_name: demo-webapp-assets
  - id: bucket-read-policy
    kind: iam:policy
    properties:
      policy_name: demo-webapp-s3-read
      document:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Action: ["s3:GetObject"]
            Resource: "${app-bucket.arn}"      # <- referencia
```

- Si la interpolación es el valor COMPLETO de la property (como arriba), el motor
  sustituye por el output tal cual.
- Si está EMBEBIDA dentro de más texto (p. ej. `"prefix-${app-bucket.name}-suffix"`),
  se concatena como string.
- `$${...}` es un ESCAPE: produce el literal `${...}` sin interpretarlo como
  referencia -- útil si una property (por ejemplo un script en `user_data_file`)
  necesita contener un `${` que no es de este motor (una variable de shell, de Make,
  etc.).
- Un id desconocido, o un output que ese recurso concreto no expone, produce un
  error explícito listando qué SÍ está disponible en ese punto -- nunca falla en
  silencio ni sustituye por una cadena vacía.

Cada `kind` documenta sus propios outputs en
[`docs/stack-manifest.md`](../docs/stack-manifest.md) (por ejemplo, `s3:bucket`
expone `name`, `arn`, `domain_name`; `iam:role` expone `arn`, `name`).

## Dependencias implícitas

`bucket-read-policy` del ejemplo de arriba NUNCA declara `depends_on: [app-bucket]` --
no hace falta. El motor extrae automáticamente cualquier `${id.key}` usado dentro de
`properties` (recursivamente, en dicts y listas anidadas) y trata ese `id` como una
dependencia, exactamente igual que si estuviera en `depends_on`. `depends_on` sigue
existiendo para el caso -- poco común -- de un recurso que depende de otro sin
necesitar ninguno de sus outputs (por ejemplo, `app-server` en `webapp-stack.yaml`
depende de `attach-policy` explícitamente: el rol tiene que tener ya la política
adjunta antes de lanzar la instancia, aunque ninguna property de la instancia
referencie un output de `attach-policy`).

El orden final de aplicación es la unión de ambas fuentes, ordenado
topológicamente -- y, ante empate, siempre alfabético por id, para que el mismo
manifiesto produzca siempre el mismo plan.

## Por qué no hay recursos de red

Ningún manifiesto de stack puede declarar una VPC, una subnet, o un security group.
`webapp-stack.yaml` REFERENCIA `corp-private-1a` y `corp-web-sg` por nombre (vía
`NetworkResolver`, de solo lectura) -- nunca los crea. Si esa subnet o ese security
group no existen, `stack apply` falla con un error explicando que ese recurso debe
solicitarse al equipo de Networking, exactamente igual que `ec2 instance launch`. Ver
la sección "Separation of Duties" de [`docs/least-privilege.md`](../docs/least-privilege.md)
para la razón completa.

`examples/network-stack.yaml` es un manifiesto (deliberadamente inválido) que
demuestra el rechazo: declara un recurso `kind: security-group:sg` y `stack validate`
lo rechaza con exit code 64, citando esa misma regla.
