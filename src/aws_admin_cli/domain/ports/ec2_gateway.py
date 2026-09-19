"""The Ec2Gateway port: EC2 instance/AMI/key-pair operations, returning domain models only.

Implementations must never leak a raw AWS SDK value across this boundary --
every method returns an ``aws_admin_cli.domain.models.ec2`` model (or
``None``/``list``/``str``/nothing), and raises only
``aws_admin_cli.core.exceptions`` types (via
``infrastructure.aws.error_mapper.aws_error_boundary``), never a raw
client-error exception from the underlying SDK.

This port is about INSTANCES, AMIs, and KEY PAIRS -- it never touches VPCs,
subnets, or security groups. EC2 *consumes* the network via
``application/services/network_resolver.py`` (read-only); it never
administers it. See ``domain/ports/vpc_gateway.py`` for that boundary, and
``tests/unit/architecture/test_vpc_readonly.py`` for the automated check that
also covers this module.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol, Self

from aws_admin_cli.domain.models.ec2 import (
    Ami,
    Instance,
    InstanceState,
    KeyMaterial,
    KeyPairInfo,
    LaunchSpec,
)


class Ec2Gateway(Protocol):
    """Port: EC2 instance, AMI, and key-pair operations."""

    def describe_images(
        self: Self,
        image_ids: Sequence[str] | None,
        owners: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Ami]:
        """List AMIs, optionally scoped by ID, owner, or filters.

        ``region`` (when given) targets that region's client instead of the
        profile-default region, and every returned ``Ami.region`` is stamped
        with it -- same convention as ``describe_instances``.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def describe_images_all_regions(
        self: Self,
        owners: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
    ) -> list[Ami]:
        """List AMIs (matching ``owners``/``filters``) across every AWS region, in parallel.

        Each returned ``Ami.region`` is set to the region it was found in. A
        region this account can't call ``DescribeImages`` against
        (unauthorized, or opted out) is skipped rather than failing the
        whole scan -- same contract as ``describe_instances_all_regions``.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def create_image(
        self: Self,
        instance_id: str,
        name: str,
        description: str | None,
        *,
        no_reboot: bool,
        tags: Mapping[str, str] | None = None,
        region: str | None = None,
    ) -> Ami:
        """Create an AMI from ``instance_id``.

        Args:
            instance_id: The source instance.
            name: The new AMI's name.
            description: Optional description.
            no_reboot: If ``True``, AWS does not shut down/reboot the
                instance before creating the image (faster, but the image
                may be inconsistent if data is being written at the time).
            tags: Optional tags applied to the new AMI at creation time (via
                ``TagSpecifications``, never a follow-up ``CreateTags`` call
                -- same "no untagged window" reasoning as instance launch).
            region: The instance's actual region, when it differs from the
                profile-default region (e.g. an instance surfaced by
                ``describe_instances_all_regions``). ``None`` uses the
                default (profile-region) client, unchanged.

        Returns:
            The newly created ``Ami``.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def copy_image(
        self: Self,
        source_image_id: str,
        *,
        source_region: str,
        name: str,
        description: str | None,
        target_region: str,
        kms_key_id: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> Ami:
        """Copy ``source_image_id`` from ``source_region`` into ``target_region``.

        Args:
            source_image_id: The AMI to copy.
            source_region: The region the source AMI lives in.
            name: The copy's name.
            description: Optional description.
            target_region: The region to copy the AMI into -- this is where
                the request itself is sent (``CopyImage``'s destination is
                the region of the client that calls it).
            kms_key_id: Optional CMK to encrypt the copy with; omitted
                entirely from the request when not given (never an empty
                string).
            tags: Optional tags applied to the copy at creation time, same
                ``TagSpecifications`` contract as :meth:`create_image`.

        Returns:
            The newly created ``Ami``, in ``target_region``.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def deregister_image(self: Self, image_id: str, *, region: str | None = None) -> None:
        """Deregister an AMI. Does not delete its backing EBS snapshot(s).

        ``region``, when given, routes the call through that region's client
        instead of the profile-default one.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def describe_instances(
        self: Self,
        instance_ids: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Instance]:
        """List instances (fully paginated), optionally scoped by ID or filters.

        ``region`` (when given) targets that region's client instead of the
        profile-default region -- used to resolve an instance the global,
        every-region scan found outside the active session region.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def describe_instances_all_regions(
        self: Self, filters: Mapping[str, Sequence[str]] | None
    ) -> list[Instance]:
        """List instances (matching ``filters``) across every AWS region, in parallel.

        Each returned ``Instance.region`` is set to the region it was found
        in. A region this account can't call ``DescribeInstances`` against
        (unauthorized, or opted out) is skipped rather than failing the
        whole scan.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def get_instance(self: Self, instance_id: str, *, region: str | None = None) -> Instance:
        """Fetch a single instance by ID.

        ``region``, when given, routes the lookup through that region's
        client instead of the profile-default one.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def create_key_pair(self: Self, key_name: str, key_type: str) -> KeyMaterial:
        """Create a new key pair; AWS generates and returns the private key exactly once."""
        ...  # pragma: no cover -- Protocol body, never executed

    def import_key_pair(self: Self, key_name: str, public_key_material: str) -> KeyPairInfo:
        """Import an existing public key as a named key pair (no private material returned)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def describe_key_pairs(self: Self, key_names: Sequence[str] | None) -> list[KeyPairInfo]:
        """List key pairs (metadata only), optionally scoped by name."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_key_pair(self: Self, key_name: str) -> None:
        """Delete a key pair."""
        ...  # pragma: no cover -- Protocol body, never executed

    def run_instance(self: Self, spec: LaunchSpec, *, client_token: str, dry_run: bool) -> Instance:
        """Launch an instance from ``spec``.

        Args:
            spec: The fully-resolved launch parameters.
            client_token: Idempotency token -- a retried call with the same
                token within AWS's dedup window returns the same instance
                instead of creating a second one.
            dry_run: If ``True``, validates permissions/parameters without
                actually launching anything.

        Returns:
            The launched ``Instance`` -- or, if ``dry_run`` is ``True`` and
            validation succeeded, a placeholder result signaling that success
            (never an exception: a successful dry-run is not an error).
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def start_instances(
        self: Self, instance_ids: Sequence[str], *, region: str | None = None
    ) -> list[str]:
        """Start stopped instances. Returns the IDs AWS accepted."""
        ...  # pragma: no cover -- Protocol body, never executed

    def stop_instances(
        self: Self, instance_ids: Sequence[str], *, force: bool, region: str | None = None
    ) -> list[str]:
        """Stop running instances. Returns the IDs AWS accepted."""
        ...  # pragma: no cover -- Protocol body, never executed

    def reboot_instances(
        self: Self, instance_ids: Sequence[str], *, region: str | None = None
    ) -> None:
        """Reboot running instances."""
        ...  # pragma: no cover -- Protocol body, never executed

    def terminate_instances(
        self: Self, instance_ids: Sequence[str], *, dry_run: bool, region: str | None = None
    ) -> list[str]:
        """Terminate instances. Returns the IDs AWS accepted."""
        ...  # pragma: no cover -- Protocol body, never executed

    def create_tags(
        self: Self,
        instance_ids: Sequence[str],
        tags: Mapping[str, str],
        *,
        region: str | None = None,
    ) -> None:
        """Add tags to instances, overwriting any existing tag that shares a key."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_tags(
        self: Self, instance_ids: Sequence[str], keys: Sequence[str], *, region: str | None = None
    ) -> None:
        """Remove tags by key from instances (values, if any, are ignored)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def set_instance_security_groups(
        self: Self, instance_id: str, group_ids: Sequence[str], *, region: str | None = None
    ) -> None:
        """Replace the security groups attached to a running instance.

        An INSTANCE-attribute change (which already-existing group IDs this
        instance uses), not a network mutation (it never creates, deletes, or
        authorizes a security group) -- see this module's own docstring and
        ``tests/unit/architecture/test_vpc_readonly.py`` for the boundary this
        stays inside of.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def wait_for_state(
        self: Self,
        instance_ids: Sequence[str],
        target: InstanceState,
        *,
        timeout_s: int,
        poll_s: int,
        region: str | None = None,
    ) -> None:
        """Block until every instance in ``instance_ids`` reaches ``target``.

        Raises:
            OperationTimeoutError: ``timeout_s`` elapsed before every instance
                reached ``target``.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def get_console_output(self: Self, instance_id: str) -> str:
        """Fetch an instance's console output (useful for debugging a failed boot)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def create_snapshot(self: Self, volume_id: str, description: str | None) -> str:
        """Create an EBS snapshot of ``volume_id``. Returns the new snapshot's ID."""
        ...  # pragma: no cover -- Protocol body, never executed
