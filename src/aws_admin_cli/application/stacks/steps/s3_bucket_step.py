"""Step: ``s3:bucket`` -- adapts a manifest resource to Create/List/DeleteBucketUseCase.

Properties: ``bucket_name``, optional ``region`` (defaults to the active
profile's region), ``enable_versioning``, ``allow_public``. Outputs:
``name``, ``arn``, ``domain_name``.
"""

from dataclasses import dataclass
from typing import Any, ClassVar, Self

from aws_admin_cli.application.dto.s3 import CreateBucketRequest, DeleteBucketRequest
from aws_admin_cli.application.stacks.steps._shared import (
    optional_bool,
    optional_str,
    require_str,
)
from aws_admin_cli.application.stacks.steps.base import StepResult
from aws_admin_cli.application.use_cases.s3.create_bucket import CreateBucketUseCase
from aws_admin_cli.application.use_cases.s3.delete_bucket import DeleteBucketUseCase
from aws_admin_cli.application.use_cases.s3.list_buckets import ListBucketsUseCase
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, ResourceState


def _bucket_outputs(name: str) -> dict[str, str]:
    return {
        "name": name,
        "arn": f"arn:aws:s3:::{name}",
        "domain_name": f"{name}.s3.amazonaws.com",
    }


@dataclass(frozen=True, slots=True)
class S3BucketStep:
    """Adapts a manifest's ``s3:bucket`` resource to Create/List/DeleteBucketUseCase."""

    kind: ClassVar[ResourceKind] = ResourceKind.S3_BUCKET

    list_use_case: ListBucketsUseCase
    create_use_case: CreateBucketUseCase
    delete_use_case: DeleteBucketUseCase
    default_region: str

    def execute(self: Self, spec: ResourceSpec, props: dict[str, Any]) -> StepResult:
        """Create the bucket, or reuse it (``created_by_stack=False``) if it already exists."""
        bucket_name = require_str(props, "bucket_name", spec.id)
        region = optional_str(props, "region") or self.default_region
        enable_versioning = optional_bool(props, "enable_versioning")
        allow_public = optional_bool(props, "allow_public")

        existed_before = any(b.name == bucket_name for b in self.list_use_case.execute())
        bucket = self.create_use_case.execute(
            CreateBucketRequest(
                name=bucket_name,
                region=region,
                allow_public=allow_public,
                enable_versioning=enable_versioning,
                if_not_exists=True,
            )
        )
        return StepResult(
            physical_id=bucket.name,
            arn=f"arn:aws:s3:::{bucket.name}",
            outputs=_bucket_outputs(bucket.name),
            created_by_stack=not existed_before,
        )

    def compensate(self: Self, state: ResourceState) -> None:
        """Delete the bucket (``--force``: empties it first)."""
        if state.physical_id is None:
            return
        self.delete_use_case.execute(DeleteBucketRequest(name=state.physical_id, force=True))

    def still_exists(self: Self, state: ResourceState) -> bool:
        """Whether the bucket is still there (drift detection)."""
        if state.physical_id is None:
            return False
        return any(b.name == state.physical_id for b in self.list_use_case.execute())
