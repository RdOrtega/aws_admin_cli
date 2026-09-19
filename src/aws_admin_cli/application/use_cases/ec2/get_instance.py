"""Use case: resolve a human reference (instance-id or tag Name) to one Instance."""

import re
from dataclasses import dataclass
from typing import Self

from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.ec2 import Instance
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway

_INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8,17}$")


@dataclass(frozen=True, slots=True)
class GetInstanceUseCase:
    """Resolve an instance reference: an ID, or an exact Name tag.

    Same ambiguity/not-found treatment as ``NetworkResolver``: an ID never
    ambiguates; a Name shared by several instances raises ``ValidationError``
    listing every candidate's ID and state, so the caller can pick one by ID
    instead of guessing.
    """

    gateway: Ec2Gateway

    def execute(self: Self, ref: str, *, region: str | None = None) -> Instance:
        """Return the instance matching ``ref``.

        ``region``, when given, routes the lookup through that instance's
        actual region instead of the profile-default one -- used to refresh
        an instance surfaced by the global, every-region scan.

        Raises:
            ResourceNotFoundError: No instance matches ``ref``.
            ValidationError: More than one instance shares the Name tag ``ref``.
        """
        if _INSTANCE_ID_RE.match(ref):
            return self.gateway.get_instance(ref, region=region)

        candidates = [
            i for i in self.gateway.describe_instances(None, None, region=region) if i.name == ref
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            listed = ", ".join(f"{i.instance_id} ({i.state.value})" for i in candidates)
            raise ValidationError(
                f"'{ref}' es ambiguo: varias instancias comparten ese nombre.",
                hint=f"Usa el instance-id para desambiguar. Candidatas: {listed}.",
            )
        raise ResourceNotFoundError(
            f"No se encontró ninguna instancia con id o tag Name '{ref}'.",
            hint="Usa `ec2 instance list` para ver las instancias disponibles.",
        )
