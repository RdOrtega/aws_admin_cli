"""El puerto ``VpcGateway``: SOLO LECTURA, por diseño -- no por descuido.

La red base (VPCs, subnets, security groups, route tables, internet/NAT
gateways) es responsabilidad del equipo de Networking/SecOps, no de
``aws_admin_cli``. Esta herramienta puede LEER esos recursos -- para auditar,
para resolver referencias humanas a IDs (ver
``application/services/network_resolver.py``), para que la Fase 5 (EC2) sepa
en qué subnet lanzar una instancia -- pero nunca puede crearlos, modificarlos
ni borrarlos. Esa separación de responsabilidades (Separation of Duties) es
deliberada: quien administra el perímetro de red y quien administra cargas de
trabajo dentro de él no deberían ser el mismo control.

Por eso este ``Protocol`` expone EXCLUSIVAMENTE métodos ``describe_*``. Añadir
aquí (o en cualquier implementación de este puerto, o en cualquier caso de uso
o comando de la CLI que lo consuma) un método que cree, modifique, autorice,
revoque, asocie, adjunte, reemplace, actualice o borre cualquier recurso de
red es una violación de arquitectura de esta fase -- lo verifica
automáticamente ``tests/unit/architecture/test_vpc_readonly.py``, que falla la
build si aparece un método con un prefijo prohibido (``create_``,
``delete_``, ``modify_``, ``authorize_``, ``revoke_``, ``associate_``,
``attach_``, ``replace_``, ``update_``, ``put_``, entre otros) en este
Protocol, en su implementación boto3, o en la CLI de este módulo.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol, Self

from aws_admin_cli.domain.models.vpc import (
    AvailabilityZone,
    RouteTable,
    SecurityGroup,
    Subnet,
    Vpc,
)


class VpcGateway(Protocol):
    """Puerto: consultas de red de solo lectura (VPCs, subnets, security groups, ...)."""

    def describe_vpcs(
        self: Self,
        vpc_ids: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Vpc]:
        """Listar VPCs, opcionalmente acotado por ID o por filtros estilo EC2.

        ``region``, si se indica, enruta la consulta al cliente de esa región
        en vez del cliente por defecto del perfil -- para resolver la red de
        una instancia encontrada fuera de la región activa de la sesión.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def describe_subnets(
        self: Self,
        subnet_ids: Sequence[str] | None,
        vpc_id: str | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Subnet]:
        """Listar subnets, opcionalmente acotado por ID, por VPC, o por filtros."""
        ...  # pragma: no cover -- Protocol body, never executed

    def describe_security_groups(
        self: Self,
        group_ids: Sequence[str] | None,
        vpc_id: str | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[SecurityGroup]:
        """Listar security groups, opcionalmente acotado por ID, por VPC, o por filtros."""
        ...  # pragma: no cover -- Protocol body, never executed

    def describe_route_tables(
        self: Self, vpc_id: str | None, *, region: str | None = None
    ) -> list[RouteTable]:
        """Listar las route tables de una VPC (o de todas, si ``vpc_id`` es ``None``)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def describe_availability_zones(self: Self) -> list[AvailabilityZone]:
        """Listar las availability zones disponibles en la región configurada."""
        ...  # pragma: no cover -- Protocol body, never executed
