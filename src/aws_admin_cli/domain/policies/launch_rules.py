"""EC2 launch/lifecycle guard rails: pure functions, same OCP spirit as the SG audit rules.

Each check is a standalone function that either returns (allowed) or raises a
domain ``ValidationError`` (blocked) -- no shared base class, no registry
needed here since these aren't findings to aggregate, they're gates a launch
or a destructive operation must pass through. Zero I/O, zero boto3, zero
logging: pure functions of their inputs.
"""

from typing import Final

from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.ec2 import Instance

DEFAULT_ALLOWED_FAMILIES: Final[frozenset[str]] = frozenset({"t2", "t3", "t3a", "m5", "m6i"})


def _family_of(instance_type: str) -> str:
    return instance_type.split(".", 1)[0]


def check_instance_type(
    instance_type: str, allowed_families: frozenset[str], *, confirmed: bool
) -> None:
    """Block launching an instance type outside the allowed family list, unless confirmed.

    Args:
        instance_type: e.g. ``"m5.24xlarge"``.
        allowed_families: Family prefixes (e.g. ``{"t2", "t3"}``) that never
            need confirmation.
        confirmed: Whether the caller passed ``--confirm-large``.

    Raises:
        ValidationError: ``instance_type``'s family isn't allowed and
            ``confirmed`` is ``False``.
    """
    family = _family_of(instance_type)
    if family in allowed_families or confirmed:
        return
    raise ValidationError(
        f"'{instance_type}' es de la familia '{family}', fuera de las familias "
        f"permitidas por defecto ({', '.join(sorted(allowed_families))}).",
        hint=f"Si de verdad quieres lanzar un '{instance_type}' (puede tener un coste "
        "significativamente mayor que las familias estándar), repite el comando con "
        "--confirm-large.",
    )


def check_public_ip(
    assign_public_ip: bool, subnet_is_public: bool | None, *, confirmed: bool
) -> None:
    """Guard against a public-IP request that's pointless or unconfirmed.

    Args:
        assign_public_ip: Whether ``--public-ip`` was passed.
        subnet_is_public: The target subnet's computed visibility (``None``
            if it couldn't be determined -- see ``domain/models/vpc.py``).
        confirmed: Whether the caller passed ``--confirm-public``.

    Raises:
        ValidationError: A public IP was requested for a subnet known to be
            private (it would never actually get one -- AWS silently ignores
            the request), or a public IP was requested without ``--confirm-public``.
    """
    if not assign_public_ip:
        return
    if subnet_is_public is False:
        raise ValidationError(
            "Se pidió --public-ip pero la subnet resuelta es privada: la instancia no "
            "recibiría una IP pública aunque se lo pidas (no tiene ruta a un internet "
            "gateway).",
            hint="Lanza en una subnet pública, o quita --public-ip.",
        )
    if not confirmed:
        raise ValidationError(
            "Se pidió --public-ip sin --confirm-public.",
            hint="Una IP pública expone la instancia a internet. Si es intencional, "
            "repite el comando con --confirm-public.",
        )


def check_managed_tag(instance: Instance, *, force: bool) -> None:
    """Refuse a destructive operation on an instance this CLI didn't create, unless ``force``.

    The single most important safeguard in this module: without it, a typo
    in an instance ID/name could terminate someone else's production
    instance. This CLI only touches what it tagged ``ManagedBy=aws-admin-cli``
    itself, unless the caller explicitly overrides that with ``--force``.

    Args:
        instance: The instance about to be acted on.
        force: Whether the caller passed ``--force``.

    Raises:
        ValidationError: ``instance`` lacks the ``ManagedBy=aws-admin-cli``
            tag and ``force`` is ``False``.
    """
    if instance.managed_by_cli or force:
        return
    raise ValidationError(
        f"La instancia '{instance.display_name}' ({instance.instance_id}) no tiene el "
        "tag ManagedBy=aws-admin-cli: esta CLI no la creó.",
        hint="Si de verdad quieres operar sobre una instancia que esta CLI no gestiona, "
        "repite el comando con --force.",
    )
