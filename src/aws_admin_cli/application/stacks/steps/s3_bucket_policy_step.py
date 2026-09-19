"""Step: ``s3:bucket-policy`` -- adapts a manifest resource to Set/DeleteBucketPolicyUseCase.

Properties: ``bucket_name``, one of ``document``/``document_file``,
optional ``allow_public``. No meaningful outputs (a policy has no ARN/id of
its own to expose).
"""

from dataclasses import dataclass
from typing import Any, ClassVar, Self

from aws_admin_cli.application.dto.s3 import SetBucketPolicyRequest
from aws_admin_cli.application.stacks.steps._shared import (
    optional_bool,
    require_str,
    resolve_policy_document,
)
from aws_admin_cli.application.stacks.steps.base import StepResult
from aws_admin_cli.application.use_cases.s3.delete_bucket_policy import DeleteBucketPolicyUseCase
from aws_admin_cli.application.use_cases.s3.get_bucket_policy import GetBucketPolicyUseCase
from aws_admin_cli.application.use_cases.s3.set_bucket_policy import SetBucketPolicyUseCase
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, ResourceState


@dataclass(frozen=True, slots=True)
class S3BucketPolicyStep:
    """Adapts a manifest's ``s3:bucket-policy`` resource to Set/Get/DeleteBucketPolicyUseCase.

    Always ``created_by_stack=True``: unlike a role or a bucket, a policy
    attachment has no independent "did this already exist" concept to
    reuse -- setting it IS this step's own action, every time it runs.
    """

    kind: ClassVar[ResourceKind] = ResourceKind.S3_BUCKET_POLICY

    set_use_case: SetBucketPolicyUseCase
    get_use_case: GetBucketPolicyUseCase
    delete_use_case: DeleteBucketPolicyUseCase

    def execute(self: Self, spec: ResourceSpec, props: dict[str, Any]) -> StepResult:
        """Set the bucket's policy."""
        bucket_name = require_str(props, "bucket_name", spec.id)
        document = resolve_policy_document(props, spec.id)
        allow_public = optional_bool(props, "allow_public")

        self.set_use_case.execute(
            SetBucketPolicyRequest(name=bucket_name, document=document, allow_public=allow_public)
        )
        return StepResult(
            physical_id=bucket_name,
            arn=None,
            outputs={"bucket_name": bucket_name},
            created_by_stack=True,
        )

    def compensate(self: Self, state: ResourceState) -> None:
        """Remove the bucket's policy."""
        if state.physical_id is None:
            return
        self.delete_use_case.execute(state.physical_id)

    def still_exists(self: Self, state: ResourceState) -> bool:
        """Whether the bucket still has a policy set (drift detection)."""
        if state.physical_id is None:
            return False
        return self.get_use_case.execute(state.physical_id) is not None
