# Política IAM de mínimo privilegio

Este documento lista, módulo por módulo, las acciones de IAM que `aws_admin_cli`
necesita para funcionar contra una cuenta real de AWS -- ni una acción más. Cada
bloque JSON es copiable directamente como `Statement` de una policy de IAM (ajusta
`Resource` a tus ARNs reales; los ejemplos usan `"*"` donde AWS no admite scoping por
ARN, y un patrón de ARN concreto donde sí lo admite).

Las acciones se derivaron directamente de las llamadas reales que hace cada gateway
(`infrastructure/aws/gateways/boto3_*_gateway.py`), no de una lista genérica -- si una
acción no aparece aquí, esta CLI no la usa.

## Módulo IAM (`iam user` / `iam policy` / `iam role`)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AwsAdminCliIamUsers",
      "Effect": "Allow",
      "Action": [
        "iam:CreateUser",
        "iam:GetUser",
        "iam:ListUsers",
        "iam:DeleteUser"
      ],
      "Resource": "arn:aws:iam::*:user/*"
    },
    {
      "Sid": "AwsAdminCliIamPolicies",
      "Effect": "Allow",
      "Action": [
        "iam:CreatePolicy",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
        "iam:ListPolicies",
        "iam:DeletePolicy"
      ],
      "Resource": "arn:aws:iam::*:policy/*"
    },
    {
      "Sid": "AwsAdminCliIamRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:GetRole",
        "iam:ListRoles",
        "iam:DeleteRole"
      ],
      "Resource": "arn:aws:iam::*:role/*"
    },
    {
      "Sid": "AwsAdminCliIamAttachments",
      "Effect": "Allow",
      "Action": [
        "iam:AttachUserPolicy",
        "iam:DetachUserPolicy",
        "iam:ListAttachedUserPolicies",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:ListAttachedRolePolicies"
      ],
      "Resource": [
        "arn:aws:iam::*:user/*",
        "arn:aws:iam::*:role/*"
      ]
    }
  ]
}
```

No incluye `iam:*`, `iam:PutUserPolicy` (políticas inline -- esta CLI solo gestiona
policies administradas), ni ninguna acción sobre `iam:PermissionsBoundary`,
grupos, MFA, o claves de acceso: la herramienta no los toca.

## Módulo S3 (`s3 bucket` / `s3 ls` / `s3 cp` / `s3 rm` / `s3 presign`)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AwsAdminCliS3AccountLevel",
      "Effect": "Allow",
      "Action": "s3:ListAllMyBuckets",
      "Resource": "*"
    },
    {
      "Sid": "AwsAdminCliS3BucketLevel",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "s3:GetBucketLocation",
        "s3:GetBucketVersioning",
        "s3:PutBucketVersioning",
        "s3:GetBucketPolicy",
        "s3:PutBucketPolicy",
        "s3:DeleteBucketPolicy",
        "s3:GetBucketTagging",
        "s3:PutBucketTagging",
        "s3:PutBucketPublicAccessBlock"
      ],
      "Resource": "arn:aws:s3:::*"
    },
    {
      "Sid": "AwsAdminCliS3ObjectLevel",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload"
      ],
      "Resource": "arn:aws:s3:::*/*"
    }
  ]
}
```

Notas:

- `s3:GetObject`/`s3:PutObject` cubren tanto `s3 cp` (subida/descarga, incluyendo
  multipart para archivos grandes) como `s3 cp` s3→s3 (`CopyObject` necesita
  `GetObject` sobre el origen y `PutObject` sobre el destino) como las URLs
  prefirmadas que genera `s3 presign` (la firma en sí no llama a AWS, pero quien
  use la URL resultante sí necesita el permiso correspondiente).
- `s3:DeleteObject` cubre tanto el borrado de un objeto como el borrado en lote
  (`DeleteObjects`) que usan `s3 rm --recursive` y `s3 bucket delete --force`.
- No incluye `s3:PutBucketAcl`, `s3:PutObjectAcl`, ni ninguna acción de
  replicación, lifecycle, encryption o logging: esta CLI no las gestiona.

## Módulo VPC (`vpc list` / `vpc show` / `vpc subnet` / `vpc sg` / `vpc az` / `vpc resolve`)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AwsAdminCliVpcReadOnly",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeRouteTables",
        "ec2:DescribeAvailabilityZones"
      ],
      "Resource": "*"
    }
  ]
}
```

**Ni una acción de mutación.** Nada de `ec2:CreateVpc`, `ec2:CreateSubnet`,
`ec2:CreateSecurityGroup`, `ec2:AuthorizeSecurityGroupIngress`,
`ec2:AuthorizeSecurityGroupEgress`, `ec2:RevokeSecurityGroup*`,
`ec2:CreateRoute*`, `ec2:AssociateRouteTable`, `ec2:CreateInternetGateway`,
`ec2:AttachInternetGateway`, ni ninguna otra acción de escritura sobre `ec2:*`
relacionada con red. `Resource: "*"` es obligatorio aquí, no una concesión de
alcance: las acciones `Describe*` de EC2 no admiten scoping por ARN de recurso
(es una limitación del propio modelo de permisos de EC2, no una elección de
esta herramienta).

## Módulo EC2 (`ec2 ami` / `ec2 keypair` / `ec2 instance`)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AwsAdminCliEc2ReadOnly",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeImages",
        "ec2:DescribeInstances",
        "ec2:DescribeKeyPairs"
      ],
      "Resource": "*"
    },
    {
      "Sid": "AwsAdminCliEc2KeyPairs",
      "Effect": "Allow",
      "Action": [
        "ec2:CreateKeyPair",
        "ec2:ImportKeyPair",
        "ec2:DeleteKeyPair"
      ],
      "Resource": "arn:aws:ec2:*:*:key-pair/*"
    },
    {
      "Sid": "AwsAdminCliEc2Launch",
      "Effect": "Allow",
      "Action": "ec2:RunInstances",
      "Resource": "*"
    },
    {
      "Sid": "AwsAdminCliEc2Lifecycle",
      "Effect": "Allow",
      "Action": [
        "ec2:StartInstances",
        "ec2:StopInstances",
        "ec2:RebootInstances",
        "ec2:TerminateInstances",
        "ec2:GetConsoleOutput"
      ],
      "Resource": "arn:aws:ec2:*:*:instance/*"
    },
    {
      "Sid": "AwsAdminCliEc2InstanceProfiles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateInstanceProfile",
        "iam:GetInstanceProfile",
        "iam:ListInstanceProfiles",
        "iam:DeleteInstanceProfile",
        "iam:AddRoleToInstanceProfile",
        "iam:RemoveRoleFromInstanceProfile"
      ],
      "Resource": "arn:aws:iam::*:instance-profile/*"
    },
    {
      "Sid": "AwsAdminCliEc2PassRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::*:role/*",
      "Condition": {
        "StringEquals": { "iam:PassedToService": "ec2.amazonaws.com" }
      }
    }
  ]
}
```

Notas:

- `ec2:RunInstances` no admite acotar `Resource` a un único ARN útil aquí: la
  llamada toca simultáneamente los recursos `instance`, `image`, `subnet`,
  `security-group`, `network-interface`, `volume` y (si aplica) `key-pair`
  implicados en el lanzamiento, y AWS evalúa cada uno contra la policy por
  separado. `Resource: "*"` es la forma práctica de conceder esto; si tu
  organización necesita acotarlo más, hazlo con `Condition` (por ejemplo,
  `ec2:InstanceType` para replicar en IAM el guard rail de familias que ya
  aplica `domain/policies/launch_rules.py`), no intentando enumerar ARNs.
- `ec2:StartInstances`/`StopInstances`/`RebootInstances`/`TerminateInstances`
  SÍ admiten scoping por ARN de instancia -- se acotan aquí a
  `instance/*` en vez de `*` porque, a diferencia de `RunInstances`, no hay
  razón para no hacerlo. (La salvaguarda real de "no toques lo que no
  gestionaste" -- `domain/policies/launch_rules.check_managed_tag` -- vive en
  esta CLI, no en la policy de IAM; si tu organización quiere reforzarla
  también a nivel de IAM, añade una `Condition` sobre
  `ec2:ResourceTag/ManagedBy`.)
- **`iam:PassRole` es la acción más peligrosa de este bloque si se deja sin
  acotar.** Sin `Resource` ni `Condition`, un caller con permiso para lanzar
  instancias podría adjuntar CUALQUIER rol de la cuenta a la instancia que
  lanza -- incluido un rol con privilegios administrativos que ese caller no
  tiene por sí mismo. Eso es una escalada de privilegios clásica: "no puedo
  actuar como administrador directamente, pero puedo lanzar una instancia con
  el rol de administrador adjunto y usar su IMDS para obtener esas
  credenciales". Por eso `Resource` está acotado a los roles que esta CLI
  puede pasar (ajusta el patrón a los roles reales de tu organización, no lo
  dejes en `arn:aws:iam::*:role/*` salvo que de verdad quieras decir "todos"),
  y la `Condition` sobre `iam:PassedToService` asegura que ese permiso solo
  aplica cuando el rol se pasa a EC2, no a cualquier otro servicio.
- Las acciones de instance profile son las que usa
  `application/use_cases/iam/ensure_instance_profile_for_role.py`
  (Fase 5) al resolver `--iam-role`: crea el instance profile si falta y le
  adjunta el rol, de forma idempotente -- nunca crea el rol en sí (eso sigue
  siendo `iam role create`, del módulo IAM).
- No incluye `ec2:CreateSecurityGroup`, `ec2:AuthorizeSecurityGroupIngress`,
  `ec2:CreateSubnet`, `ec2:CreateVpc`, ni ninguna otra acción de mutación de
  red: ver la sección "Separation of Duties" más abajo -- EC2 consume la red
  que resuelve el módulo `vpc`, nunca la administra.

## Núcleo (`doctor`)

No forma parte de ningún módulo, pero el comando `doctor` (Fase 1) valida
credenciales llamando a STS:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AwsAdminCliDoctor",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
```

## Separation of Duties

El módulo `vpc` es deliberadamente de solo lectura. La red base de una cuenta --
VPCs, subnets, security groups, route tables, internet/NAT gateways -- es un
recurso compartido por todo lo que corre en esa cuenta: un cambio ahí (una regla
de security group demasiado permisiva, una subnet reasignada, una route table mal
asociada) no afecta a un solo equipo o a una sola carga de trabajo, afecta al
perímetro de todos. Por eso, en la práctica, ese perímetro casi siempre lo
administra un equipo dedicado (Networking o SecOps), con su propio proceso de
revisión, separado de los equipos que administran identidades (IAM) o
almacenamiento (S3) para sus propias cargas de trabajo.

`aws_admin_cli` refleja esa separación en el software, no solo en la
documentación: el `Protocol` `VpcGateway` (`domain/ports/vpc_gateway.py`) no
expone ni un solo método de escritura, su implementación boto3
(`infrastructure/aws/gateways/boto3_vpc_gateway.py`) no hace ni una sola llamada
mutante, y `presentation/cli/vpc_app.py` nunca importa `confirm_destructive`
porque no hay nada destructivo que confirmar. Esto no es una convención de
estilo: lo verifica automáticamente
`tests/unit/architecture/test_vpc_readonly.py`, que falla la build si aparece un
método `create_*`/`delete_*`/`modify_*`/`authorize_*`/`revoke_*`/`associate_*`/
`attach_*`/`replace_*`/`update_*`/`put_*` en cualquiera de esas tres piezas.

El módulo EC2 (Fase 5) hereda la misma regla en vez de repetirla: `Ec2Gateway`,
`Boto3Ec2Gateway` y `presentation/cli/ec2_app.py` legítimamente llaman a
operaciones `create_`/`start_`/`stop_`/`terminate_` -- pero solo sobre
INSTANCIAS, key pairs y AMIs, nunca sobre VPCs/subnets/security groups. Ese
mismo test de arquitectura amplía la comprobación a EC2: falla si aparece
`create_security_group`, `authorize_security_group*`, `create_subnet`,
`create_vpc`, `modify_subnet` o `revoke_security_group*` en cualquiera de esos
tres archivos, o en `application/use_cases/ec2/`.

En la práctica, esto significa:

- Si necesitas una VPC, subnets, o security groups nuevos: pídeselos a
  Networking/SecOps (o a quien administre la infraestructura de red en tu
  organización), igual que le pedirías una IP pública o una ruta BGP.
- `aws_admin_cli` te ayuda a **auditar** lo que ya existe (`vpc sg audit`) y a
  **consumirlo** de forma legible por humanos (`vpc resolve`, y desde la Fase 5,
  las banderas `--subnet`/`--sg` de `ec2 instance launch`, resueltas por
  `application/services/network_resolver.py`) -- nunca a cambiarlo.
- Si `ec2 instance launch --sg` apunta a un security group que no existe, el
  comando falla con `ResourceNotFoundError` (exit code 4) y un hint que dice
  que ese recurso debe pedirse a Networking -- nunca lo crea automáticamente,
  por tentador que sea "simplemente crear el SG que falta" en ese momento.
- Si en algún momento un caso de uso pareciera necesitar crear o modificar un
  recurso de red para funcionar, eso es una señal de que ese caso de uso no
  pertenece al módulo `vpc` (ni a `ec2`): pertenece a Networking/SecOps, fuera
  de esta herramienta.
