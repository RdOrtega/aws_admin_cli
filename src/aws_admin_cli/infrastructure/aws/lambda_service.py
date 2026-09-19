"""Basic AWS Lambda scaffolding.

Lists functions (globally, across every region) and reads one function's
runtime/handler/environment-variable configuration.

Deliberately NOT layered through ``domain/ports`` + a dedicated Gateway +
``application/use_cases`` like EC2/S3/IAM/VPC -- this is the first cut of the
Lambda module (read-only), kept to exactly the two files this feature asked
for: this one for the AWS calls, and ``presentation/tui/flows/lambda_flow.py``
for the TUI. A fuller Lambda feature set (deploy, invoke, update
configuration) would earn the same Protocol/Gateway/UseCase layering
everything else in this project has; this scaffolding doesn't need it yet.
Still routes every botocore call through :func:`aws_error_boundary`, same as
every real gateway, so nothing raw ever reaches ``lambda_flow.py`` (which,
like every other flow, never imports ``boto3``/``botocore`` itself -- see
``tests/unit/architecture/test_tui_boundaries.py``).
"""

import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Self

from aws_admin_cli.core.exceptions import AwsError
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.error_mapper import aws_error_boundary

_logger = logging.getLogger("aws_admin_cli")


@dataclass(frozen=True, slots=True)
class LambdaFunctionSummary:
    """One Lambda function, as returned by ``ListFunctions``, plus the region it's in."""

    function_name: str
    runtime: str
    region: str
    memory_size_mb: int | None = None
    timeout_s: int | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class LambdaFunctionDetail:
    """One function's full configuration: runtime, handler, and environment variables."""

    function_name: str
    runtime: str
    region: str
    handler: str
    memory_size_mb: int | None
    timeout_s: int | None
    environment: Mapping[str, str]


def _to_summary(raw: Mapping[str, Any], region: str) -> LambdaFunctionSummary:
    return LambdaFunctionSummary(
        function_name=raw["FunctionName"],
        runtime=raw.get("Runtime", "unknown"),
        region=region,
        memory_size_mb=raw.get("MemorySize"),
        timeout_s=raw.get("Timeout"),
        last_modified=raw.get("LastModified"),
    )


@dataclass(frozen=True, slots=True)
class LambdaService:
    """Read-only Lambda operations.

    Lists functions (one region or every region), and fetches one function's
    runtime/handler/environment variables.
    """

    client_factory: ClientFactory

    def _client(self: Self, *, region: str | None = None) -> Any:
        return self.client_factory.lambda_client(region=region)

    def list_functions(self: Self, *, region: str | None = None) -> list[LambdaFunctionSummary]:
        """List every function in one region (the profile-default region, unless given)."""
        target_region = region or self.client_factory.settings.region
        functions: list[LambdaFunctionSummary] = []
        with aws_error_boundary("lambda", "ListFunctions"):
            paginator = self._client(region=region).get_paginator("list_functions")
            for page in paginator.paginate():
                functions.extend(
                    _to_summary(raw, target_region) for raw in page.get("Functions", [])
                )
        return functions

    def _list_regions(self: Self) -> list[str]:
        """Every region name available to this account (opted-in + always-on).

        Lambda has no ``ListFunctions``-adjacent "list regions" API of its
        own, so this borrows EC2's ``DescribeRegions`` -- the same call
        ``Boto3Ec2Gateway._list_regions`` makes for the identical purpose.
        """
        with aws_error_boundary("ec2", "DescribeRegions"):
            response = self.client_factory.ec2().describe_regions(AllRegions=False)
        return [region["RegionName"] for region in response.get("Regions", [])]

    def list_functions_all_regions(self: Self) -> list[LambdaFunctionSummary]:
        """List functions across every AWS region, fetched in parallel.

        Mirrors ``Boto3Ec2Gateway.describe_instances_all_regions``'s pattern:
        a region this account can't call ``ListFunctions`` against
        (unauthorized, opted out, or otherwise erroring) is skipped with a
        logged warning rather than failing the whole scan.
        """
        regions = self._list_regions()
        functions: list[LambdaFunctionSummary] = []
        with ThreadPoolExecutor(max_workers=len(regions) or 1) as executor:
            future_to_region = {
                executor.submit(self.list_functions, region=region): region for region in regions
            }
            for future in as_completed(future_to_region):
                region = future_to_region[future]
                try:
                    functions.extend(future.result())
                except AwsError as exc:
                    _logger.warning(
                        "Skipping region '%s' (ListFunctions failed): %s", region, exc
                    )
        return functions

    def get_function(self: Self, name: str, *, region: str | None = None) -> LambdaFunctionDetail:
        """Fetch one function's runtime/handler/environment variables."""
        with aws_error_boundary("lambda", "GetFunctionConfiguration"):
            response = self._client(region=region).get_function_configuration(FunctionName=name)
        return LambdaFunctionDetail(
            function_name=response["FunctionName"],
            runtime=response.get("Runtime", "unknown"),
            region=region or self.client_factory.settings.region,
            handler=response.get("Handler", ""),
            memory_size_mb=response.get("MemorySize"),
            timeout_s=response.get("Timeout"),
            environment=response.get("Environment", {}).get("Variables", {}),
        )
