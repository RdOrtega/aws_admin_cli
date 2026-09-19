"""Use case: create an EC2 key pair, saving its private material to a local file, safely."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from aws_admin_cli.application.dto.ec2 import CreateKeyPairRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.ec2 import KeyPairInfo
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway

_PRIVATE_KEY_FILE_MODE = 0o600


@dataclass(frozen=True, slots=True)
class CreateKeyPairUseCase:
    """Create a key pair and save its private material locally -- exactly once, never overwritten.

    AWS returns a key pair's private material exactly once, at creation
    time; there is no way to retrieve it again afterward. So this use case
    checks the destination file doesn't already exist BEFORE calling AWS at
    all (fail fast, no orphaned AWS-side key pair on a doomed local write),
    then writes it atomically with ``O_CREAT | O_EXCL`` (closing the race
    between that check and the write) at mode ``0600`` -- readable only by
    the file's owner, exactly what SSH itself requires of a private key file.
    """

    gateway: Ec2Gateway

    def execute(self: Self, request: CreateKeyPairRequest) -> tuple[Path, KeyPairInfo]:
        """Create ``request.name`` and save it under ``request.destination_dir``.

        Returns:
            The path the private key was saved to, and the key pair's
            metadata (name, ID, fingerprint) -- NEVER the private material
            itself; callers must not log or render it.

        Raises:
            ValidationError: The destination file already exists.
        """
        destination = request.destination_dir / f"{request.name}.pem"
        if destination.exists():
            raise ValidationError(
                f"Ya existe un archivo en '{destination}'.",
                hint="Elige otro --path o nombre de key pair, o borra el archivo "
                "existente tú mismo si estás seguro de que ya no lo necesitas.",
            )

        material = self.gateway.create_key_pair(request.name, request.key_type)
        try:
            self._write_private_key(destination, material.private_key)
        except FileExistsError as exc:
            # Lost the TOCTOU race: something created the file between our check
            # above and this write. The AWS-side key pair's private material is
            # now unrecoverable if we don't clean it up -- delete it rather than
            # leave an orphaned key pair nobody can ever get the material for.
            self.gateway.delete_key_pair(request.name)
            raise ValidationError(
                f"Ya existe un archivo en '{destination}'.",
                hint="Elige otro --path o nombre de key pair.",
            ) from exc

        infos = self.gateway.describe_key_pairs([request.name])
        info = (
            infos[0]
            if infos
            else KeyPairInfo(
                key_name=request.name, key_pair_id="", key_fingerprint="", key_type=request.key_type
            )
        )
        return destination, info

    def _write_private_key(self: Self, destination: Path, private_key: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, _PRIVATE_KEY_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(private_key)
