"""The ``StackStep`` contract: one adapter per ``ResourceKind``, between the engine and a use case.

A step's job, and ONLY job, is translation: interpolated manifest
``properties`` in, a use-case request out; a use-case result in, a
``StepResult`` out. A step must never call a gateway directly, and must
never re-implement a validation or guard rail a use case already owns --
``tests/unit/architecture/test_stack_no_network.py`` and this module's own
docstring are the standing reminder of why (see ``docs/architecture.md``'s
saga-pattern section for the full rationale).
"""

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, Self

from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, ResourceState

__all__ = ["StackStep", "StepResult"]


@dataclass(frozen=True, slots=True)
class StepResult:
    """What a step's ``execute`` reports back to the engine.

    Attributes:
        physical_id: The AWS-side identifier (role name, bucket name,
            instance id, ...) -- whatever a human would use to find this
            resource in the AWS console.
        arn: The resource's ARN, if it has one (an EC2 instance's "ARN" is
            usually left ``None`` here -- outputs still carry its instance
            id via a kind-specific key).
        outputs: Values other resources can reference via ``${this.key}``.
            Always ``dict[str, str]`` -- see
            ``domain/services/interpolation.py``'s module docstring for why
            outputs are string-only.
        created_by_stack: ``True`` if THIS apply actually created the
            resource; ``False`` if a use case found it already existed (by
            name) and reused it. This is the single field that decides
            whether ``compensate``/``destroy`` will EVER touch this
            resource -- see ``application/stacks/engine.py``'s module
            docstring.
    """

    physical_id: str | None
    arn: str | None
    outputs: dict[str, str]
    created_by_stack: bool


class StackStep(Protocol):
    """Port: one resource kind's adapter between a manifest and its use case(s).

    Every concrete step is a frozen dataclass holding the use case(s) it
    delegates to (injected by ``application/stacks/registry.py``), never a
    gateway or a ``ClientFactory`` -- constructing a step never touches the
    network any more than constructing a use case does.
    """

    kind: ClassVar[ResourceKind]

    def execute(self: Self, spec: ResourceSpec, props: dict[str, Any]) -> StepResult:
        """Create (or idempotently reuse) the resource ``spec`` describes.

        Args:
            spec: The resource's manifest declaration (id, kind, raw
                ``depends_on`` -- NOT interpolated properties).
            props: ``spec.properties`` with every ``${id.key}`` reference
                already resolved against prior steps' outputs.

        Returns:
            What was created/reused, and whether this call is the one that
            created it.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def compensate(self: Self, state: ResourceState) -> None:
        """Undo ``execute``'s effect for a resource THIS apply created.

        Only ever called by the engine for a ``ResourceState`` whose
        ``created_by_stack`` is ``True`` -- a step's ``compensate`` must be
        safe to assume that invariant and never re-check it itself.

        Args:
            state: The persisted state of the resource to compensate --
                carries whatever ``execute`` returned (``physical_id``,
                ``arn``, ``outputs``), since that's all a step has to work
                with to undo itself (the original ``ResourceSpec``/``props``
                may no longer even be available, e.g. during ``destroy``).
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def still_exists(self: Self, state: ResourceState) -> bool:
        """Return whether the resource ``state`` describes still exists in AWS.

        The read-only counterpart to ``execute``/``compensate``, used only by
        ``StackEngine.status(..., refresh=True)`` for drift detection --
        never called during ``apply``/``destroy``. Implemented the same way
        each step already checks for pre-existence inside ``execute``
        (a ``Get``/``List`` use case, never a raw gateway call).
        """
        ...  # pragma: no cover -- Protocol body, never executed
