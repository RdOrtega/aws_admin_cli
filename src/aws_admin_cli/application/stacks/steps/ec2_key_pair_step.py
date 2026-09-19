"""Step: ``ec2:key-pair`` -- adapts a manifest resource to Create/List/DeleteKeyPairUseCase.

Properties: ``key_name``, optional ``save_path`` (defaults to ``~/.ssh``).
Outputs: ``key_name``, ``fingerprint``, ``path`` -- NEVER the private key
material. ``path`` is safe to expose (it's where the ``.pem`` was saved, not
its contents) and lets another resource's ``user_data`` reference it if it
ever needs to.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Self

from aws_admin_cli.application.dto.ec2 import CreateKeyPairRequest
from aws_admin_cli.application.stacks.steps._shared import optional_str, require_str
from aws_admin_cli.application.stacks.steps.base import StepResult
from aws_admin_cli.application.use_cases.ec2.create_key_pair import CreateKeyPairUseCase
from aws_admin_cli.application.use_cases.ec2.delete_key_pair import DeleteKeyPairUseCase
from aws_admin_cli.application.use_cases.ec2.list_key_pairs import ListKeyPairsUseCase
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, ResourceState


@dataclass(frozen=True, slots=True)
class Ec2KeyPairStep:
    """Adapts a manifest's ``ec2:key-pair`` resource to Create/List/DeleteKeyPairUseCase."""

    kind: ClassVar[ResourceKind] = ResourceKind.EC2_KEY_PAIR

    list_use_case: ListKeyPairsUseCase
    create_use_case: CreateKeyPairUseCase
    delete_use_case: DeleteKeyPairUseCase
    default_save_dir: Path

    def execute(self: Self, spec: ResourceSpec, props: dict[str, Any]) -> StepResult:
        """Create the key pair, or reuse it (``created_by_stack=False``) if it already exists.

        AWS never returns a key pair's private material again after
        creation -- reusing an existing key pair means no local ``.pem`` is
        written at all (there's nothing new to save), only its public
        metadata is reported.
        """
        key_name = require_str(props, "key_name", spec.id)
        save_path = optional_str(props, "save_path")
        destination_dir = Path(save_path) if save_path else self.default_save_dir

        existing = next(
            (info for info in self.list_use_case.execute() if info.key_name == key_name), None
        )
        if existing is not None:
            return StepResult(
                physical_id=existing.key_name,
                arn=None,
                outputs={"key_name": existing.key_name, "fingerprint": existing.key_fingerprint},
                created_by_stack=False,
            )

        destination, info = self.create_use_case.execute(
            CreateKeyPairRequest(name=key_name, destination_dir=destination_dir)
        )
        return StepResult(
            physical_id=info.key_name,
            arn=None,
            outputs={
                "key_name": info.key_name,
                "fingerprint": info.key_fingerprint,
                "path": str(destination),
            },
            created_by_stack=True,
        )

    def compensate(self: Self, state: ResourceState) -> None:
        """Delete the key pair from AWS. Never touches the local ``.pem`` file."""
        if state.physical_id is None:
            return
        self.delete_use_case.execute(state.physical_id)

    def still_exists(self: Self, state: ResourceState) -> bool:
        """Whether the key pair is still there (drift detection)."""
        if state.physical_id is None:
            return False
        return any(info.key_name == state.physical_id for info in self.list_use_case.execute())
