"""The stack engine: manifest -> plan -> apply/destroy, with saga-pattern rollback.

See ``docs/architecture.md``'s saga-pattern section for the full rationale;
the two rules that matter most while reading this module:

1. **``created_by_stack`` is the only thing that decides whether rollback or
   destroy EVER touches a resource.** A step's ``execute`` reports it in its
   ``StepResult``; the engine copies it verbatim onto the persisted
   ``ResourceState`` and never recomputes or second-guesses it. A resource
   this apply merely found-and-reused (``created_by_stack=False``) is never
   compensated, never destroyed -- not on this apply's own rollback, not on
   a later ``stack destroy``.
2. **Compensation is sequential and individually wrapped.** Rollback walks
   the resources THIS apply actually touched, in exact reverse order, and
   compensates one at a time -- never in parallel, never batched. If one
   compensation fails, it's recorded (``COMPENSATION_FAILED``) and rollback
   moves on to the next one regardless; the original failure is never lost
   (it's always ``StackApplyError.__cause__``), and every resource that
   ended up neither compensated nor untouched-by-design is reported back as
   ``orphaned``.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Self

from aws_admin_cli.application.stacks.steps.base import StackStep
from aws_admin_cli.core.exceptions import ResourceNotFoundError, StackApplyError, StackNotFoundError
from aws_admin_cli.domain.models.stack import (
    ResourceKind,
    ResourceSpec,
    ResourceState,
    ResourceStatus,
    StackManifest,
    StackState,
    StackStatus,
    compute_manifest_hash,
    compute_resource_fingerprint,
)
from aws_admin_cli.domain.ports.repository import Repository
from aws_admin_cli.domain.services.dependency_graph import destroy_order, topological_order
from aws_admin_cli.domain.services.interpolation import resolve_properties

__all__ = [
    "OnEvent",
    "PlannedAction",
    "PlannedResource",
    "StackEngine",
    "StackEvent",
    "StackPlan",
]


class PlannedAction(str, Enum):
    """What ``StackEngine.plan`` decided would happen to one resource."""

    CREATE = "create"
    NO_OP = "no-op"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class PlannedResource:
    """One resource's planned action, in the order ``apply`` would process it."""

    order: int
    logical_id: str
    kind: ResourceKind
    action: PlannedAction
    depends_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StackPlan:
    """The full, ordered plan ``apply`` would follow -- a domain object, not text.

    ``presentation/cli/stack_app.py`` is the only thing that turns this into
    a table or JSON; the engine that built it knows nothing about rendering.
    """

    stack_name: str
    resources: tuple[PlannedResource, ...]

    @property
    def has_changes(self: Self) -> bool:
        """Whether at least one resource would actually be touched (not every action is NO_OP)."""
        return any(resource.action is not PlannedAction.NO_OP for resource in self.resources)


@dataclass(frozen=True, slots=True)
class StackEvent:
    """One progress notification the engine emits mid-``apply``/``destroy``.

    The engine knows nothing about Rich or consoles: ``on_event`` is a plain
    callback, so ``presentation/cli/stack_app.py`` can render it however it
    wants (and tests can just collect a list of them).
    """

    phase: str  # "apply" | "rollback" | "destroy"
    logical_id: str
    kind: ResourceKind
    status: ResourceStatus
    detail: str | None = None


OnEvent = Callable[[StackEvent], None]


def _noop_on_event(event: StackEvent) -> None:
    del event


def _upsert(resources: list[ResourceState], new: ResourceState) -> list[ResourceState]:
    """Replace ``new`` in place by ``logical_id`` (preserving position), or append it.

    Preserving position matters: ``resources`` must stay in topological
    (apply) order for ``destroy_order`` to correctly reverse it later --
    reordering on update would silently corrupt that invariant.
    """
    result = list(resources)
    for index, existing in enumerate(result):
        if existing.logical_id == new.logical_id:
            result[index] = new
            return result
    result.append(new)
    return result


@dataclass(slots=True)
class StackEngine:
    """Applies/destroys a ``StackManifest`` against real use cases, with rollback on failure.

    ``profile``/``region`` are carried here (not on individual method calls)
    for the same reason ``CreateRoleUseCase``/``CreateBucketUseCase``/etc.
    carry them: every persisted ``StackState`` needs to record which
    profile/region it was applied against, and this is the one place that
    builds every ``StackState`` this module writes.
    """

    steps: Mapping[ResourceKind, StackStep]
    repository: Repository[StackState]
    logger: logging.Logger
    profile: str
    region: str

    def plan(self: Self, manifest: StackManifest) -> StackPlan:
        """Compute the ordered CREATE/NO-OP/REPLACE plan ``apply`` would follow. No I/O."""
        order = topological_order(manifest)
        previous = self.repository.get(manifest_key(manifest.name))
        planned = [
            PlannedResource(
                order=index,
                logical_id=resource.id,
                kind=resource.kind,
                action=self._plan_action(resource, previous),
                depends_on=tuple(sorted(resource.depends_on)),
            )
            for index, resource in enumerate(order)
        ]
        return StackPlan(stack_name=manifest.name, resources=tuple(planned))

    def _plan_action(
        self: Self, resource: ResourceSpec, previous: StackState | None
    ) -> PlannedAction:
        if previous is None:
            return PlannedAction.CREATE
        prior = previous.get_resource(resource.id)
        if prior is None or prior.status is not ResourceStatus.CREATED:
            return PlannedAction.CREATE
        if prior.fingerprint != compute_resource_fingerprint(resource):
            return PlannedAction.REPLACE
        return PlannedAction.NO_OP

    def apply(
        self: Self,
        manifest: StackManifest,
        *,
        dry_run: bool = False,
        no_rollback: bool = False,
        on_event: OnEvent = _noop_on_event,
    ) -> StackState:
        """Apply ``manifest``: resolve, execute each step in order, roll back on failure.

        Args:
            manifest: The validated manifest to apply.
            dry_run: If ``True``, validates and previews the plan only --
                zero AWS calls, zero state writes. Returns a ``StackState``
                shaped like the real one would be (status ``PLANNED``), but
                never persisted.
            no_rollback: If ``True``, a failure leaves every resource
                created so far exactly as-is (status ``FAILED`` for the
                stack, no compensation attempted) -- for debugging a failure
                in place. Logs a WARNING naming every resource left behind.
            on_event: Called for every step transition (CREATING, CREATED,
                FAILED, COMPENSATING, COMPENSATED, COMPENSATION_FAILED, ...)
                -- the CLI's only way to show progress; purely a
                notification, the engine ignores whatever it returns.

        Returns:
            The final persisted ``StackState`` on success.

        Raises:
            ValidationError: The manifest has an unresolvable dependency, a
                cycle, or a step raised one validating its own properties.
            StackApplyError: A step failed. ``__cause__`` is the original
                exception; ``.orphaned`` lists every ``ResourceState`` this
                apply created that rollback did NOT clean up (empty if
                rollback fully succeeded).
        """
        order = topological_order(manifest)
        previous = self.repository.get(manifest_key(manifest.name))
        manifest_hash = compute_manifest_hash(manifest)
        now = datetime.now(UTC)

        if dry_run:
            return self._preview(manifest, order, previous, manifest_hash, now)

        resources: list[ResourceState] = list(previous.resources) if previous is not None else []
        outputs: dict[str, dict[str, str]] = {
            r.logical_id: r.outputs for r in resources if r.status is ResourceStatus.CREATED
        }
        created_at = previous.created_at if previous is not None else now

        def _persist(status: StackStatus, current_resources: list[ResourceState]) -> StackState:
            state = StackState(
                name=manifest.name,
                status=status,
                profile=self.profile,
                region=self.region,
                manifest_hash=manifest_hash,
                created_at=created_at,
                updated_at=datetime.now(UTC),
                resources=list(current_resources),
            )
            self.repository.save(state)
            return state

        _persist(StackStatus.APPLYING, resources)

        touched: list[ResourceSpec] = []
        failure: Exception | None = None
        failed_resource_id: str | None = None

        for resource in order:
            fingerprint = compute_resource_fingerprint(resource)
            prior = next((r for r in resources if r.logical_id == resource.id), None)
            if (
                prior is not None
                and prior.status is ResourceStatus.CREATED
                and prior.fingerprint == fingerprint
            ):
                on_event(
                    StackEvent("apply", resource.id, resource.kind, ResourceStatus.SKIPPED)
                )
                continue

            step = self.steps[resource.kind]
            on_event(StackEvent("apply", resource.id, resource.kind, ResourceStatus.CREATING))
            try:
                props = resolve_properties(resource.properties, outputs)
                result = step.execute(resource, props)
            except Exception as exc:  # any failure here triggers rollback below
                failure = exc
                failed_resource_id = resource.id
                # A REPLACE that fails during resolution/validation (before anything new
                # was actually created) must not erase a still-alive PRIOR resource's
                # physical_id/arn/outputs/created_by_stack -- that old resource is still
                # exactly as stack-owned (or not) as it was before this failed attempt, and
                # `stack destroy` still needs to be able to clean it up later. This entry is
                # never itself eligible for THIS apply's own orphan/rollback accounting
                # regardless of created_by_stack, because that's scoped to `touched` (see
                # `orphaned = [... if r.logical_id in touched_ids ...]` below) and a FAILED
                # status is never CREATED/COMPENSATION_FAILED in the first place.
                resources = _upsert(
                    resources,
                    ResourceState(
                        logical_id=resource.id,
                        kind=resource.kind,
                        status=ResourceStatus.FAILED,
                        physical_id=prior.physical_id if prior else None,
                        arn=prior.arn if prior else None,
                        outputs=prior.outputs if prior else {},
                        created_by_stack=prior.created_by_stack if prior else False,
                        error=str(exc),
                    ),
                )
                _persist(StackStatus.FAILED, resources)
                on_event(
                    StackEvent(
                        "apply", resource.id, resource.kind, ResourceStatus.FAILED, str(exc)
                    )
                )
                break

            resources = _upsert(
                resources,
                ResourceState(
                    logical_id=resource.id,
                    kind=resource.kind,
                    status=ResourceStatus.CREATED,
                    physical_id=result.physical_id,
                    arn=result.arn,
                    outputs=result.outputs,
                    created_by_stack=result.created_by_stack,
                    created_at=datetime.now(UTC),
                    fingerprint=fingerprint,
                ),
            )
            outputs[resource.id] = result.outputs
            touched.append(resource)
            # Persist BEFORE notifying: an observer told CREATED must be able to trust
            # that the state is already durably on disk, not still in-flight.
            _persist(StackStatus.APPLYING, resources)
            on_event(StackEvent("apply", resource.id, resource.kind, ResourceStatus.CREATED))

        if failure is None:
            return _persist(StackStatus.APPLIED, resources)

        if no_rollback:
            self.logger.warning(
                "El apply de '%s' falló en '%s' y no_rollback=True: los recursos ya creados "
                "NO se compensan y siguen existiendo en AWS. Revisa `stack status %s`.",
                manifest.name,
                failed_resource_id,
                manifest.name,
            )
            touched_ids = {r.id for r in touched}
            orphaned = [
                r
                for r in resources
                if r.logical_id in touched_ids
                and r.created_by_stack
                and r.status is ResourceStatus.CREATED
            ]
            raise StackApplyError(
                f"El apply de '{manifest.name}' falló en '{failed_resource_id}'. No se hizo "
                "rollback (--no-rollback): los recursos que ya se crearon siguen en AWS.",
                hint=f"Revisa `stack status {manifest.name}` y límpialos a mano si hace falta.",
                orphaned=orphaned,
            ) from failure

        def _persist_during_rollback(current: list[ResourceState]) -> None:
            _persist(StackStatus.FAILED, current)

        resources = self._rollback(touched, resources, on_event, persist=_persist_during_rollback)
        incomplete = any(r.status is ResourceStatus.COMPENSATION_FAILED for r in resources)
        _persist(
            StackStatus.ROLLBACK_INCOMPLETE if incomplete else StackStatus.ROLLED_BACK, resources
        )

        touched_ids = {r.id for r in touched}
        orphaned = [
            r
            for r in resources
            if r.logical_id in touched_ids
            and r.created_by_stack
            and r.status in (ResourceStatus.CREATED, ResourceStatus.COMPENSATION_FAILED)
        ]
        raise StackApplyError(
            f"El apply de '{manifest.name}' falló en '{failed_resource_id}'; "
            f"rollback {'incompleto' if incomplete else 'completo'}.",
            hint=(
                "Hay recursos huérfanos que necesitan limpieza manual (ver .orphaned)."
                if orphaned
                else "El rollback limpió todo lo que este apply había creado. Resuelve el "
                "error original y reintenta `stack apply`."
            ),
            orphaned=orphaned,
        ) from failure

    def _rollback(
        self: Self,
        touched: list[ResourceSpec],
        resources: list[ResourceState],
        on_event: OnEvent,
        *,
        persist: Callable[[list[ResourceState]], None],
    ) -> list[ResourceState]:
        """Compensate ``touched`` in exact reverse order; never touch a reused resource.

        Persists after EVERY compensation attempt (success or failure), same
        crash-durability guarantee ``apply``'s forward loop makes -- if the
        process dies mid-rollback, the state file must reflect exactly which
        resources have already been compensated.
        """
        for resource in reversed(touched):
            state = next(r for r in resources if r.logical_id == resource.id)
            if not state.created_by_stack or state.status is not ResourceStatus.CREATED:
                continue

            step = self.steps[resource.kind]
            on_event(
                StackEvent("rollback", resource.id, resource.kind, ResourceStatus.COMPENSATING)
            )
            try:
                step.compensate(state)
            except Exception as exc:  # a cleanup failure must never abort rollback
                resources = _upsert(
                    resources,
                    state.model_copy(
                        update={"status": ResourceStatus.COMPENSATION_FAILED, "error": str(exc)}
                    ),
                )
                persist(resources)
                on_event(
                    StackEvent(
                        "rollback",
                        resource.id,
                        resource.kind,
                        ResourceStatus.COMPENSATION_FAILED,
                        str(exc),
                    )
                )
            else:
                compensated = state.model_copy(
                    update={"status": ResourceStatus.COMPENSATED, "error": None}
                )
                resources = _upsert(resources, compensated)
                persist(resources)
                on_event(
                    StackEvent(
                        "rollback", resource.id, resource.kind, ResourceStatus.COMPENSATED
                    )
                )
        return resources

    def _preview(
        self: Self,
        manifest: StackManifest,
        order: list[ResourceSpec],
        previous: StackState | None,
        manifest_hash: str,
        now: datetime,
    ) -> StackState:
        """Build a ``dry_run`` preview: never persisted, never calls a step.

        Interpolation isn't attempted here: it can only be validated by
        actually running a step (to learn its real outputs), which a dry
        run by definition never does. A resource unchanged since the last
        real apply keeps its true, already-known state; every other
        resource is previewed as ``PENDING`` with no outputs.
        """
        resources: list[ResourceState] = []
        for resource in order:
            fingerprint = compute_resource_fingerprint(resource)
            prior = previous.get_resource(resource.id) if previous is not None else None
            if (
                prior is not None
                and prior.status is ResourceStatus.CREATED
                and prior.fingerprint == fingerprint
            ):
                resources.append(prior)
            else:
                resources.append(
                    ResourceState(
                        logical_id=resource.id,
                        kind=resource.kind,
                        status=ResourceStatus.PENDING,
                        created_by_stack=False,
                        fingerprint=fingerprint,
                    )
                )
        return StackState(
            name=manifest.name,
            status=StackStatus.PLANNED,
            profile=self.profile,
            region=self.region,
            manifest_hash=manifest_hash,
            created_at=previous.created_at if previous is not None else now,
            updated_at=now,
            resources=resources,
        )

    def destroy(
        self: Self, stack_name: str, *, force: bool = False, on_event: OnEvent = _noop_on_event
    ) -> StackState:
        """Destroy every ``created_by_stack=True`` resource, in reverse topological order.

        Args:
            stack_name: The stack to destroy.
            force: Whether a compensation failure (other than the resource
                already being gone -- that's never an error, regardless of
                ``force``) should be recorded and skipped past (``True``) or
                should halt destruction of whatever's left (``False``, the
                safer default: stop and let the operator look before more
                resources are touched).
            on_event: Same progress-notification contract as ``apply``.

        Raises:
            StackNotFoundError: No persisted state exists for ``stack_name``.
        """
        loaded = self.repository.get(manifest_key(stack_name))
        if loaded is None:
            raise StackNotFoundError(
                f"No hay ningún stack con estado persistido llamado '{stack_name}'.",
                hint="Usa `stack list` para ver los stacks conocidos.",
            )
        base_state: StackState = loaded

        resources = list(base_state.resources)

        def _persist(status: StackStatus, current_resources: list[ResourceState]) -> StackState:
            new_state = base_state.model_copy(
                update={
                    "status": status,
                    "resources": list(current_resources),
                    "updated_at": datetime.now(UTC),
                }
            )
            self.repository.save(new_state)
            return new_state

        _persist(StackStatus.DESTROYING, resources)

        all_clean = True
        for resource_state in destroy_order(resources):
            if not resource_state.created_by_stack:
                continue
            if resource_state.status in (ResourceStatus.DESTROYED, ResourceStatus.COMPENSATED):
                continue

            step = self.steps[resource_state.kind]
            on_event(
                StackEvent(
                    "destroy",
                    resource_state.logical_id,
                    resource_state.kind,
                    ResourceStatus.COMPENSATING,
                )
            )
            try:
                step.compensate(resource_state)
            except ResourceNotFoundError:
                # Already gone -- not an error, regardless of `force`: a resource that
                # no longer exists doesn't need destroying.
                destroyed = resource_state.model_copy(
                    update={"status": ResourceStatus.DESTROYED, "error": None}
                )
                resources = _upsert(resources, destroyed)
                _persist(StackStatus.DESTROYING, resources)
                on_event(
                    StackEvent(
                        "destroy",
                        resource_state.logical_id,
                        resource_state.kind,
                        ResourceStatus.DESTROYED,
                        "ya no existía en AWS",
                    )
                )
            except Exception as exc:  # recorded, never aborts unless not force
                all_clean = False
                resources = _upsert(
                    resources,
                    resource_state.model_copy(
                        update={"status": ResourceStatus.COMPENSATION_FAILED, "error": str(exc)}
                    ),
                )
                _persist(StackStatus.DESTROYING, resources)
                on_event(
                    StackEvent(
                        "destroy",
                        resource_state.logical_id,
                        resource_state.kind,
                        ResourceStatus.COMPENSATION_FAILED,
                        str(exc),
                    )
                )
                if not force:
                    break
            else:
                destroyed = resource_state.model_copy(
                    update={"status": ResourceStatus.DESTROYED, "error": None}
                )
                resources = _upsert(resources, destroyed)
                _persist(StackStatus.DESTROYING, resources)
                on_event(
                    StackEvent(
                        "destroy",
                        resource_state.logical_id,
                        resource_state.kind,
                        ResourceStatus.DESTROYED,
                    )
                )

        # No "destroy incomplete" member exists on StackStatus (see domain/models/stack.py) --
        # leaving status DESTROYING when cleanup is incomplete reads correctly either way:
        # literally "still being destroyed", i.e. "run `stack destroy` again".
        return _persist(StackStatus.DESTROYED if all_clean else StackStatus.DESTROYING, resources)

    def status(self: Self, stack_name: str, *, refresh: bool = False) -> StackState:
        """Return the persisted state for ``stack_name``, optionally refreshed against AWS.

        Args:
            stack_name: The stack to look up.
            refresh: If ``True``, checks every ``CREATED`` resource against
                AWS (via each step's ``still_exists``) and marks any that
                have drifted -- deleted or changed outside this tool -- as
                ``FAILED`` with an explanatory ``error`` (there's no
                dedicated "drifted" status in ``ResourceStatus``; ``FAILED``
                plus that message is the closest accurate fit). Persists the
                refreshed state only if drift was actually found.

        Raises:
            StackNotFoundError: No persisted state exists for ``stack_name``.
        """
        state = self.repository.get(manifest_key(stack_name))
        if state is None:
            raise StackNotFoundError(
                f"No hay ningún stack con estado persistido llamado '{stack_name}'.",
                hint="Usa `stack list` para ver los stacks conocidos.",
            )
        if not refresh:
            return state

        resources: list[ResourceState] = []
        drifted = False
        for resource_state in state.resources:
            if resource_state.status is not ResourceStatus.CREATED:
                resources.append(resource_state)
                continue
            step = self.steps[resource_state.kind]
            if step.still_exists(resource_state):
                resources.append(resource_state)
            else:
                drifted = True
                resources.append(
                    resource_state.model_copy(
                        update={
                            "status": ResourceStatus.FAILED,
                            "error": "Drift: el recurso ya no existe en AWS (o fue modificado "
                            "fuera de este stack).",
                        }
                    )
                )

        if not drifted:
            return state
        new_state = state.model_copy(
            update={"resources": resources, "updated_at": datetime.now(UTC)}
        )
        self.repository.save(new_state)
        return new_state


def manifest_key(stack_name: str) -> str:
    """Repository key for ``stack_name`` -- mirrors ``StackState.key``, no instance needed."""
    return f"stack:{stack_name}"
