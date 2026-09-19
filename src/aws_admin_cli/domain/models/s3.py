"""S3 domain models.

Same hydration pattern as ``domain/models/iam.py``: PascalCase alias
generator matching AWS's own casing, ``populate_by_name=True`` so snake_case
kwargs work too (tests, or building a value about to be sent to AWS), and
``extra="ignore"`` because AWS routinely adds response fields we don't model.

Two fields need an explicit alias override because ``to_pascal`` doesn't
happen to match AWS's actual casing: ``etag`` (AWS: ``"ETag"``, not
``"Etag"``) and ``mfa_delete`` (AWS: ``"MFADelete"``, not ``"MfaDelete"``).
"""

import ipaddress
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_pascal

from aws_admin_cli.core.exceptions import ValidationError

logger = logging.getLogger("aws_admin_cli")

_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="ignore",
    populate_by_name=True,
    alias_generator=to_pascal,
)

_MIN_BUCKET_NAME_LENGTH = 3
_MAX_BUCKET_NAME_LENGTH = 63
_ALLOWED_CHARS_RE = re.compile(r"^[a-z0-9.-]+$")
_STARTS_ENDS_RE = re.compile(r"^[a-z0-9].*[a-z0-9]$")
_FORBIDDEN_PREFIXES = ("xn--", "sthree-", "amzn-s3-demo-")
_FORBIDDEN_SUFFIXES = ("-s3alias", "--ol-s3", ".mrap")


def validate_bucket_name(name: str) -> str:
    """Validate a bucket name against AWS's full S3 bucket-naming rule set.

    Checked client-side so a bad name fails immediately, before any network
    call, with the same domain ``ValidationError`` AWS's own rejection would
    eventually produce anyway. Each violation names the specific rule broken,
    never a generic "invalid name".

    Args:
        name: Candidate bucket name.

    Returns:
        ``name`` unchanged, if valid.

    Raises:
        ValidationError: Any of AWS's bucket-naming rules is violated.
    """
    if not (_MIN_BUCKET_NAME_LENGTH <= len(name) <= _MAX_BUCKET_NAME_LENGTH):
        raise ValidationError(
            f"'{name}' no es un nombre de bucket válido: debe tener entre "
            f"{_MIN_BUCKET_NAME_LENGTH} y {_MAX_BUCKET_NAME_LENGTH} caracteres "
            f"(tiene {len(name)}).",
            hint="Elige un nombre de entre 3 y 63 caracteres.",
        )
    if not _ALLOWED_CHARS_RE.match(name):
        raise ValidationError(
            f"'{name}' no es un nombre de bucket válido: solo se permiten minúsculas, "
            "dígitos, puntos (.) y guiones (-).",
            hint="Quita mayúsculas, espacios o símbolos no permitidos.",
        )
    if not _STARTS_ENDS_RE.match(name):
        raise ValidationError(
            f"'{name}' no es un nombre de bucket válido: debe empezar y terminar con "
            "una letra minúscula o un dígito.",
            hint="No empieces ni termines el nombre con '.' o '-'.",
        )
    if ".." in name:
        raise ValidationError(
            f"'{name}' no es un nombre de bucket válido: no puede contener dos puntos "
            'consecutivos ("..").',
            hint="Usa un solo punto entre cada etiqueta, como en un nombre de dominio.",
        )
    if _looks_like_ip_address(name):
        raise ValidationError(
            f"'{name}' no es un nombre de bucket válido: no puede tener el formato de "
            "una dirección IP.",
            hint="Usa un nombre descriptivo en vez de algo como 192.168.1.1.",
        )
    for prefix in _FORBIDDEN_PREFIXES:
        if name.startswith(prefix):
            raise ValidationError(
                f"'{name}' no es un nombre de bucket válido: no puede empezar con "
                f"'{prefix}' (prefijo reservado por AWS).",
                hint="Elige un prefijo distinto.",
            )
    for suffix in _FORBIDDEN_SUFFIXES:
        if name.endswith(suffix):
            raise ValidationError(
                f"'{name}' no es un nombre de bucket válido: no puede terminar en "
                f"'{suffix}' (sufijo reservado por AWS).",
                hint="Elige un sufijo distinto.",
            )
    if "." in name:
        logger.warning(
            "El nombre de bucket '%s' contiene puntos: esto rompe la verificación de "
            "certificado TLS para acceso virtual-hosted-style (*.s3.amazonaws.com). "
            "Sigue siendo un nombre válido.",
            name,
        )
    return name


def _looks_like_ip_address(name: str) -> bool:
    try:
        ipaddress.IPv4Address(name)
    except ValueError:
        return False
    return True


_S3_URI_HINT = "Usa el formato s3://bucket/clave, s3://bucket, o bucket/clave."
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


@dataclass(frozen=True, slots=True)
class S3Uri:
    """An ``s3://bucket/key``-style value object, parsed once and passed around typed.

    ``presentation/`` parses raw ``S3_URI`` CLI arguments into this via
    :meth:`parse` (in a Typer callback), so every command downstream receives
    an already-validated value object, never a raw string.
    """

    bucket: str
    key: str | None

    @classmethod
    def parse(cls: type[Self], raw: str) -> Self:
        """Parse ``raw`` as an S3 URI.

        Accepts ``"s3://bucket"``, ``"s3://bucket/"``, ``"s3://bucket/a/b.txt"``,
        and the schemeless ``"bucket/a/b.txt"`` (or bare ``"bucket"``) forms.

        Args:
            raw: The raw string, typically straight from a CLI argument.

        Returns:
            The parsed ``S3Uri``.

        Raises:
            ValidationError: ``raw`` doesn't match any of the accepted shapes.
        """
        if not raw or not raw.strip():
            raise ValidationError(f"'{raw}' no es un S3 URI válido.", hint=_S3_URI_HINT)

        text = raw
        if text.startswith("s3://"):
            text = text[len("s3://") :]
        elif _SCHEME_RE.match(text):
            raise ValidationError(
                f"'{raw}' no es un S3 URI válido: solo se admite el esquema s3://.",
                hint=_S3_URI_HINT,
            )

        if not text or text.startswith("/"):
            raise ValidationError(f"'{raw}' no es un S3 URI válido.", hint=_S3_URI_HINT)

        bucket, _, key = text.partition("/")
        if not bucket:
            raise ValidationError(
                f"'{raw}' no es un S3 URI válido: falta el nombre del bucket.",
                hint=_S3_URI_HINT,
            )
        return cls(bucket=bucket, key=key or None)

    @property
    def is_bucket_only(self: Self) -> bool:
        """Whether this URI names only a bucket, with no object key."""
        return self.key is None

    def __str__(self: Self) -> str:
        """Reconstruct the canonical ``s3://bucket[/key]`` URI."""
        if self.key is None:
            return f"s3://{self.bucket}"
        return f"s3://{self.bucket}/{self.key}"


class StorageClass(str, Enum):
    """S3 object storage class."""

    STANDARD = "STANDARD"
    STANDARD_IA = "STANDARD_IA"
    ONEZONE_IA = "ONEZONE_IA"
    INTELLIGENT_TIERING = "INTELLIGENT_TIERING"
    GLACIER = "GLACIER"
    DEEP_ARCHIVE = "DEEP_ARCHIVE"


class VersioningStatus(str, Enum):
    """A bucket's versioning configuration status."""

    ENABLED = "Enabled"
    SUSPENDED = "Suspended"
    DISABLED = "Disabled"


class Bucket(BaseModel):
    """An S3 bucket."""

    model_config = _MODEL_CONFIG

    name: str
    creation_date: datetime
    region: str | None = None


class S3Object(BaseModel):
    """An object within an S3 bucket, as returned by a listing or ``head_object``."""

    model_config = _MODEL_CONFIG

    key: str
    size: int
    last_modified: datetime
    etag: str = Field(alias="ETag")
    storage_class: StorageClass = StorageClass.STANDARD

    @property
    def human_size(self: Self) -> str:
        """Human-readable size (e.g. ``"3.4MB"``) -- presentation only, never sent to AWS."""
        size = float(self.size)
        units = ("B", "KB", "MB", "GB")
        unit = units[0]
        for candidate in units:
            unit = candidate
            if size < 1024 or candidate == units[-1]:
                break
            size /= 1024
        return f"{size:.1f}{unit}"


class ObjectListing(BaseModel):
    """A page (or, once fully paginated, the whole) result of listing a bucket's objects."""

    model_config = _MODEL_CONFIG

    objects: list[S3Object] = Field(default_factory=list)
    common_prefixes: list[str] = Field(default_factory=list)
    key_count: int = 0
    is_truncated: bool = False

    @property
    def total_size(self: Self) -> int:
        """Sum of every listed object's size, in bytes."""
        return sum(item.size for item in self.objects)


class BucketVersioning(BaseModel):
    """A bucket's versioning configuration."""

    model_config = _MODEL_CONFIG

    status: VersioningStatus = VersioningStatus.DISABLED
    mfa_delete: bool = Field(default=False, alias="MFADelete")

    @field_validator("mfa_delete", mode="before")
    @classmethod
    def _normalize_mfa_delete(cls: type[Self], value: object) -> object:
        """AWS reports this as the string "Enabled"/"Disabled", not a boolean."""
        if isinstance(value, str):
            return value == "Enabled"
        return value
