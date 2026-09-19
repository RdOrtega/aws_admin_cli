"""Step: ``iam:policy`` -- adapts a manifest resource to Create/List/DeletePolicyUseCase.

Properties: ``policy_name``, one of ``document``/``document_file``, optional
``allow_wildcard``. Outputs: ``arn``, ``name``.
"""

from dataclasses import dataclass
from typing import Any, ClassVar, Self

from aws_admin_cli.application.dto.iam import CreatePolicyRequest, DeletePolicyRequest
from aws_admin_cli.application.stacks.steps._shared import (
    optional_bool,
    require_str,
    resolve_policy_document,
)
from aws_admin_cli.application.stacks.steps.base import StepResult
from aws_admin_cli.application.use_cases.iam.create_policy import CreatePolicyUseCase
from aws_admin_cli.application.use_cases.iam.delete_policy import DeletePolicyUseCase
from aws_admin_cli.application.use_cases.iam.list_policies import ListPoliciesUseCase
from aws_admin_cli.domain.models.iam import IamPolicy
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, ResourceState


@dataclass(frozen=True, slots=True)
class IamPolicyStep:
    """Adapts a manifest's ``iam:policy`` resource to Create/List/DeletePolicyUseCase.

    IAM policies are looked up by ARN, and the ARN isn't known until AWS
    assigns one at creation -- so, unlike ``IamRoleStep`` (which can ``Get``
    by the caller-chosen name directly), pre-existence here is checked by
    listing customer-managed policies and matching on name, the same way
    ``CreateBucketUseCase._find_existing`` already does for S3 buckets.
    """

    kind: ClassVar[ResourceKind] = ResourceKind.IAM_POLICY

    list_use_case: ListPoliciesUseCase
    create_use_case: CreatePolicyUseCase
    delete_use_case: DeletePolicyUseCase

    def execute(self: Self, spec: ResourceSpec, props: dict[str, Any]) -> StepResult:
        """Create the policy, or reuse it (``created_by_stack=False``) if it already exists."""
        policy_name = require_str(props, "policy_name", spec.id)
        document = resolve_policy_document(props, spec.id)
        allow_wildcard = optional_bool(props, "allow_wildcard")

        existing = self._find_existing(policy_name)
        if existing is not None:
            return StepResult(
                physical_id=existing.arn,
                arn=existing.arn,
                outputs={"arn": existing.arn, "name": existing.policy_name},
                created_by_stack=False,
            )

        policy = self.create_use_case.execute(
            CreatePolicyRequest(name=policy_name, document=document, allow_wildcard=allow_wildcard)
        )
        return StepResult(
            physical_id=policy.arn,
            arn=policy.arn,
            outputs={"arn": policy.arn, "name": policy.policy_name},
            created_by_stack=True,
        )

    def _find_existing(self: Self, policy_name: str) -> IamPolicy | None:
        policies = self.list_use_case.execute("Local", False)
        return next((policy for policy in policies if policy.policy_name == policy_name), None)

    def compensate(self: Self, state: ResourceState) -> None:
        """Delete the policy (``--force``: also works if something is still attached).

        ``--force`` matters here specifically because rollback compensates in
        REVERSE topological order: an ``iam:policy-attachment`` depending on
        this policy is compensated (detached) first, but if THAT compensation
        itself failed (``COMPENSATION_FAILED``), this step still runs next --
        without ``force``, it would fail too instead of cleaning up what it can.
        """
        if state.arn is None:
            return
        self.delete_use_case.execute(DeletePolicyRequest(arn=state.arn, force=True))

    def still_exists(self: Self, state: ResourceState) -> bool:
        """Whether the policy is still there (drift detection)."""
        if state.arn is None:
            return False
        return any(policy.arn == state.arn for policy in self.list_use_case.execute("Local", False))
