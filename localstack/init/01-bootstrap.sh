#!/usr/bin/env bash
# ============================================================================
# Bootstrap de red simulada para LocalStack -- INFRAESTRUCTURA DE ENTORNO.
#
# Este script NO es parte de aws_admin_cli. Es un doble local de lo que, en
# una cuenta real, ya habría entregado el equipo de Networking/SecOps antes
# de que esta herramienta exista: una VPC, sus subnets públicas/privadas, el
# internet gateway, las route tables y los security groups. Que este script
# CREE recursos no viola la regla de solo lectura del módulo `vpc` de la CLI
# (ver docs/least-privilege.md, sección "Separation of Duties"): la CLI nunca
# ejecuta este script ni ninguna de las llamadas que contiene, solo lee lo que
# aquí se siembra con `vpc list` / `vpc sg audit` / etc.
#
# LocalStack ejecuta este archivo una vez al arrancar (o cuando `make seed`
# lo relanza a mano). Es idempotente: si un recurso ya existe (localizado por
# su tag Name / GroupName), se reutiliza en vez de duplicarse o fallar.
# ============================================================================
set -euo pipefail

export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

echo "== aws_admin_cli: sembrando red simulada (corp-main-vpc) =="

# -- VPC ---------------------------------------------------------------------

VPC_ID=$(awslocal ec2 describe-vpcs \
    --filters "Name=tag:Name,Values=corp-main-vpc" \
    --query "Vpcs[0].VpcId" --output text 2>/dev/null || echo "None")

if [ "$VPC_ID" = "None" ] || [ -z "$VPC_ID" ]; then
    VPC_ID=$(awslocal ec2 create-vpc \
        --cidr-block 10.0.0.0/16 \
        --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=corp-main-vpc}]' \
        --query "Vpc.VpcId" --output text)
    echo "Creada VPC $VPC_ID"
else
    echo "VPC ya existe: $VPC_ID"
fi

# -- Subnets -------------------------------------------------------------
# Nombre -> CIDR:AZ:public
declare -A SUBNETS=(
    [corp-public-1a]="10.0.1.0/24:us-east-1a:true"
    [corp-public-1b]="10.0.2.0/24:us-east-1b:true"
    [corp-private-1a]="10.0.11.0/24:us-east-1a:false"
    [corp-private-1b]="10.0.12.0/24:us-east-1b:false"
)
declare -A SUBNET_IDS

for name in "${!SUBNETS[@]}"; do
    IFS=':' read -r cidr az is_public <<<"${SUBNETS[$name]}"

    subnet_id=$(awslocal ec2 describe-subnets \
        --filters "Name=tag:Name,Values=$name" \
        --query "Subnets[0].SubnetId" --output text 2>/dev/null || echo "None")

    if [ "$subnet_id" = "None" ] || [ -z "$subnet_id" ]; then
        subnet_id=$(awslocal ec2 create-subnet \
            --vpc-id "$VPC_ID" \
            --cidr-block "$cidr" \
            --availability-zone "$az" \
            --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=$name}]" \
            --query "Subnet.SubnetId" --output text)
        echo "Creada subnet $name ($subnet_id, $cidr, $az)"
    else
        echo "Subnet ya existe: $name ($subnet_id)"
    fi

    if [ "$is_public" = "true" ]; then
        awslocal ec2 modify-subnet-attribute \
            --subnet-id "$subnet_id" \
            --map-public-ip-on-launch >/dev/null
    fi

    SUBNET_IDS[$name]="$subnet_id"
done

# -- Internet Gateway ------------------------------------------------------

IGW_ID=$(awslocal ec2 describe-internet-gateways \
    --filters "Name=tag:Name,Values=corp-main-igw" \
    --query "InternetGateways[0].InternetGatewayId" --output text 2>/dev/null || echo "None")

if [ "$IGW_ID" = "None" ] || [ -z "$IGW_ID" ]; then
    IGW_ID=$(awslocal ec2 create-internet-gateway \
        --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=corp-main-igw}]' \
        --query "InternetGateway.InternetGatewayId" --output text)
    awslocal ec2 attach-internet-gateway --vpc-id "$VPC_ID" --internet-gateway-id "$IGW_ID"
    echo "Creado e IGW $IGW_ID adjunto a $VPC_ID"
else
    echo "IGW ya existe: $IGW_ID"
fi

# -- Route table pública (0.0.0.0/0 -> IGW), asociada a las subnets públicas -

RTB_ID=$(awslocal ec2 describe-route-tables \
    --filters "Name=tag:Name,Values=corp-public-rtb" \
    --query "RouteTables[0].RouteTableId" --output text 2>/dev/null || echo "None")

if [ "$RTB_ID" = "None" ] || [ -z "$RTB_ID" ]; then
    RTB_ID=$(awslocal ec2 create-route-table \
        --vpc-id "$VPC_ID" \
        --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=corp-public-rtb}]' \
        --query "RouteTable.RouteTableId" --output text)
    echo "Creada route table pública $RTB_ID"
else
    echo "Route table pública ya existe: $RTB_ID"
fi

if ! awslocal ec2 describe-route-tables --route-table-ids "$RTB_ID" \
    --query "RouteTables[0].Routes[?DestinationCidrBlock=='0.0.0.0/0']" \
    --output text | grep -q .; then
    awslocal ec2 create-route \
        --route-table-id "$RTB_ID" \
        --destination-cidr-block 0.0.0.0/0 \
        --gateway-id "$IGW_ID" >/dev/null
    echo "Ruta 0.0.0.0/0 -> $IGW_ID añadida a $RTB_ID"
fi

for name in corp-public-1a corp-public-1b; do
    subnet_id="${SUBNET_IDS[$name]}"
    already_associated=$(awslocal ec2 describe-route-tables --route-table-ids "$RTB_ID" \
        --query "RouteTables[0].Associations[?SubnetId=='$subnet_id']" --output text)
    if [ -z "$already_associated" ]; then
        awslocal ec2 associate-route-table \
            --route-table-id "$RTB_ID" --subnet-id "$subnet_id" >/dev/null
        echo "Asociada $name a la route table pública"
    fi
done

# -- Security groups (con hallazgos deliberados para el auditor) -------------

create_sg() {
    local name="$1" description="$2"
    local sg_id
    sg_id=$(awslocal ec2 describe-security-groups \
        --filters "Name=group-name,Values=$name" "Name=vpc-id,Values=$VPC_ID" \
        --query "SecurityGroups[0].GroupId" --output text 2>/dev/null || echo "None")
    if [ "$sg_id" = "None" ] || [ -z "$sg_id" ]; then
        sg_id=$(awslocal ec2 create-security-group \
            --group-name "$name" --description "$description" --vpc-id "$VPC_ID" \
            --query "GroupId" --output text)
        echo "Creado SG $name ($sg_id)" >&2
    else
        echo "SG ya existe: $name ($sg_id)" >&2
    fi
    echo "$sg_id"
}

# Checks for a matching rule via describe (not by reacting to an
# InvalidPermission.Duplicate failure): some LocalStack versions don't reject
# a literal duplicate ingress rule the way real AWS does, so relying on the
# authorize call itself to fail would silently pile up duplicate rules on
# every re-run instead of actually being idempotent.
authorize_ingress_if_absent() {
    local sg_id="$1" protocol="$2" from_port="$3" to_port="$4" cidr="$5"
    local query existing
    if [ "$protocol" = "-1" ]; then
        query="SecurityGroups[0].IpPermissions[?IpProtocol=='-1' && IpRanges[?CidrIp=='$cidr']] | length(@)"
    else
        query="SecurityGroups[0].IpPermissions[?IpProtocol=='$protocol' && FromPort==\`$from_port\` && ToPort==\`$to_port\` && IpRanges[?CidrIp=='$cidr']] | length(@)"
    fi
    existing=$(awslocal ec2 describe-security-groups --group-ids "$sg_id" --query "$query" --output text)
    if [ "$existing" != "0" ]; then
        return 0
    fi
    if [ "$protocol" = "-1" ]; then
        awslocal ec2 authorize-security-group-ingress --group-id "$sg_id" \
            --ip-permissions "IpProtocol=-1,IpRanges=[{CidrIp=$cidr}]" >/dev/null
    else
        awslocal ec2 authorize-security-group-ingress --group-id "$sg_id" \
            --ip-permissions "IpProtocol=$protocol,FromPort=$from_port,ToPort=$to_port,IpRanges=[{CidrIp=$cidr}]" \
            >/dev/null
    fi
}

WEB_SG=$(create_sg corp-web-sg "Trafico web publico (80/443) -- correcto, no es hallazgo")
authorize_ingress_if_absent "$WEB_SG" tcp 80 80 0.0.0.0/0
authorize_ingress_if_absent "$WEB_SG" tcp 443 443 0.0.0.0/0

BASTION_SG=$(create_sg corp-bastion-sg "HALLAZGO CRITICO: SSH abierto al mundo")
authorize_ingress_if_absent "$BASTION_SG" tcp 22 22 0.0.0.0/0

DB_SG=$(create_sg corp-db-sg "HALLAZGO CRITICO: PostgreSQL abierto al mundo")
authorize_ingress_if_absent "$DB_SG" tcp 5432 5432 0.0.0.0/0

INTERNAL_SG=$(create_sg corp-internal-sg "Correcto: todos los protocolos, pero solo origen privado")
authorize_ingress_if_absent "$INTERNAL_SG" -1 "" "" 10.0.0.0/16

LEGACY_SG=$(create_sg corp-legacy-sg "HALLAZGO ALTO: rango amplio de puertos abierto al mundo")
authorize_ingress_if_absent "$LEGACY_SG" tcp 1024 65535 0.0.0.0/0

# -- Resumen -------------------------------------------------------------

echo ""
echo "== Resumen de la red simulada =="
echo "VPC:              $VPC_ID (corp-main-vpc, 10.0.0.0/16)"
echo "IGW:               $IGW_ID"
echo "Route table pub.: $RTB_ID"
for name in corp-public-1a corp-public-1b corp-private-1a corp-private-1b; do
    echo "Subnet $name: ${SUBNET_IDS[$name]}"
done
echo "SG corp-web-sg:      $WEB_SG"
echo "SG corp-bastion-sg:  $BASTION_SG"
echo "SG corp-db-sg:       $DB_SG"
echo "SG corp-internal-sg: $INTERNAL_SG"
echo "SG corp-legacy-sg:   $LEGACY_SG"
echo "== Bootstrap completo =="
