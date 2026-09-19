"""In-memory ``StackStep``/``Repository[StackState]`` test doubles.

Used instead of ``Mock()`` on purpose -- see ``tests/fakes/iam.py`` for why.
``FakeStackStep`` is deliberately configurable (fail on a given resource id's
``execute``, fail its ``compensate``, or report it as reused rather than
created) so ``test_stack_engine.py`` can drive every branch of
``StackEngine``'s rollback logic without any real use case or gateway.
"""

from dataclasses import dataclass, field
from typing import Any, Self

from aws_admin_cli.application.stacks.steps.base import StepResult
from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, ResourceState, StackState


@dataclass
class FakeStackStep:
    """A configurable ``StackStep`` double that records every ``execute``/``compensate`` call.

    ``calls`` records ``(verb, logical_id)`` pairs in the exact order the
    engine invoked them -- what every ordering assertion in
    ``test_stack_engine.py`` reads. ``live`` tracks which logical ids this
    fake currently considers "created in AWS" (added by a successful
    ``execute``, removed by a successful ``compensate``) -- what
    ``still_exists`` reports, and what a test checks at the end of a chaos
    scenario to confirm nothing was left behind.
    """

    kind: ResourceKind
    fail_execute_for: frozenset[str] = frozenset()
    fail_compensate_for: frozenset[str] = frozenset()
    not_found_compensate_for: frozenset[str] = frozenset()
    reused_ids: frozenset[str] = frozenset()
    calls: list[tuple[str, str]] = field(default_factory=list)
    live: set[str] = field(default_factory=set)
    received_props: dict[str, dict[str, Any]] = field(default_factory=dict)

    def execute(self: Self, spec: ResourceSpec, props: dict[str, Any]) -> StepResult:
        self.calls.append(("execute", spec.id))
        self.received_props[spec.id] = props
        if spec.id in self.fail_execute_for:
            raise ValidationError(f"fake: fallo simulado ejecutando '{spec.id}'.")
        created_by_stack = spec.id not in self.reused_ids
        if created_by_stack:
            self.live.add(spec.id)
        return StepResult(
            physical_id=f"physical-{spec.id}",
            arn=f"arn:fake:{spec.id}",
            outputs={"value": f"output-of-{spec.id}"},
            created_by_stack=created_by_stack,
        )

    def compensate(self: Self, state: ResourceState) -> None:
        self.calls.append(("compensate", state.logical_id))
        if state.logical_id in self.not_found_compensate_for:
            raise ResourceNotFoundError(f"fake: '{state.logical_id}' ya no existe.")
        if state.logical_id in self.fail_compensate_for:
            raise ValidationError(f"fake: fallo simulado compensando '{state.logical_id}'.")
        self.live.discard(state.logical_id)

    def still_exists(self: Self, state: ResourceState) -> bool:
        return state.logical_id in self.live


@dataclass
class InMemoryStackRepository:
    """A ``Repository[StackState]`` double backed by a plain dict."""

    items: dict[str, StackState] = field(default_factory=dict)

    def save(self: Self, item: StackState) -> None:
        self.items[item.key] = item

    def get(self: Self, key: str) -> StackState | None:
        return self.items.get(key)

    def list_all(self: Self) -> list[StackState]:
        return list(self.items.values())

    def delete(self: Self, key: str) -> bool:
        if key not in self.items:
            return False
        del self.items[key]
        return True

    def exists(self: Self, key: str) -> bool:
        return key in self.items
