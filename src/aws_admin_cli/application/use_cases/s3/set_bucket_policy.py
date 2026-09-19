"""Use case: replace a bucket's policy, guarding against accidental public exposure."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.s3 import SetBucketPolicyRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class SetBucketPolicyUseCase:
    """Set a bucket policy: blocks a public ``Principal`` unless ``allow_public`` is set.

    Same spirit as IAM's full-wildcard guard rail, reusing ``PolicyDocument``
    (``has_public_principal``) rather than reimplementing policy inspection.
    """

    gateway: S3Gateway

    def execute(self: Self, request: SetBucketPolicyRequest) -> None:
        """Set the policy.

        Raises:
            ValidationError: The document grants access to ``Principal: "*"``
                (or ``{"AWS": "*"}``) and ``allow_public`` is ``False``.
                Nothing is set in this case.
        """
        if request.document.has_public_principal() and not request.allow_public:
            raise ValidationError(
                f"La política de '{request.name}' concede acceso a Principal \"*\" "
                "-- expone el bucket a cualquiera en internet.",
                hint="Si es intencional, vuelve a intentarlo con --allow-public.",
            )
        self.gateway.set_bucket_policy(request.name, request.document)
