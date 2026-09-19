"""Tests for StackEngine -- plan/apply/destroy/status, rollback, and the chaos test.

All against ``FakeStackStep``/``InMemoryStackRepository`` -- no real use
case, no gateway, no AWS. This is the file where the saga pattern's
correctness actually gets proven.
"""

import logging

import pytest
from aws_admin_cli.application.stacks.engine import StackEngine, StackEvent
from aws_admin_cli.core.exceptions import StackApplyError, StackNotFoundError, ValidationError
from aws_admin_cli.domain.models.stack import (
    ResourceKind,
    ResourceSpec,
    ResourceStatus,
    StackManifest,
    StackStatus,
)

from tests.fakes.stack import FakeStackStep, InMemoryStackRepository

_PROFILE = "localstack"
_REGION = "us-east-1"


def _resource(
    resource_id: str,
    *,
    kind: ResourceKind = ResourceKind.IAM_ROLE,
    properties: dict[str, object] | None = None,
    depends_on: list[str] | None = None,
) -> ResourceSpec:
    return ResourceSpec(
        id=resource_id, kind=kind, properties=properties or {}, depends_on=depends_on or []
    )


def _chain_manifest(ids: list[str], name: str = "chaos-stack") -> StackManifest:
    resources = [
        _resource(rid, depends_on=[ids[index - 1]] if index > 0 else [])
        for index, rid in enumerate(ids)
    ]
    return StackManifest(apiVersion="v1", name=name, resources=resources)


def _engine(
    step: FakeStackStep, repository: InMemoryStackRepository | None = None
) -> StackEngine:
    return StackEngine(
        steps={ResourceKind.IAM_ROLE: step},
        repository=repository or InMemoryStackRepository(),
        logger=logging.getLogger("test.stack_engine"),
        profile=_PROFILE,
        region=_REGION,
    )


# -- happy path -----------------------------------------------------------------------


def test_apply_happy_path_executes_in_topological_order() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    engine = _engine(step)
    manifest = _chain_manifest(["res-a", "res-b", "res-c"])

    state = engine.apply(manifest)

    assert state.status is StackStatus.APPLIED
    assert step.calls == [("execute", "res-a"), ("execute", "res-b"), ("execute", "res-c")]
    assert all(r.status is ResourceStatus.CREATED for r in state.resources)


def test_apply_propagates_outputs_via_interpolation() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    engine = _engine(step)
    manifest = StackManifest(
        apiVersion="v1",
        name="demo",
        resources=[
            _resource("res-a"),
            _resource("res-b", properties={"ref": "${res-a.value}"}, depends_on=["res-a"]),
        ],
    )

    engine.apply(manifest)

    assert step.received_props["res-b"]["ref"] == "output-of-res-a"


# -- idempotency ------------------------------------------------------------------------


def test_reapplying_an_applied_stack_executes_nothing() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(["res-a", "res-b"])

    engine.apply(manifest)
    step.calls.clear()
    second = engine.apply(manifest)

    assert second.status is StackStatus.APPLIED
    assert step.calls == []


# -- incremental persistence --------------------------------------------------------------


def test_state_is_persisted_after_every_step_not_only_at_the_end() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(["res-a", "res-b", "res-c"])

    seen_after_second_creation: list[str] = []

    def on_event(event: StackEvent) -> None:
        if event.status is ResourceStatus.CREATED and event.logical_id == "res-b":
            persisted = repository.get("stack:chaos-stack")
            assert persisted is not None
            seen_after_second_creation.extend(r.logical_id for r in persisted.resources)

    engine.apply(manifest, on_event=on_event)

    # At the moment res-b's CREATED event fired, res-a and res-b (but not yet res-c)
    # must already be visible in the repository -- proof persistence happens per-step.
    assert seen_after_second_creation == ["res-a", "res-b"]


# -- the chaos test -----------------------------------------------------------------------


@pytest.mark.parametrize("failure_index", [0, 1, 2, 3, 4])
def test_chaos_failure_at_each_step_rolls_back_everything_before_it(failure_index: int) -> None:
    ids = ["res-0", "res-1", "res-2", "res-3", "res-4"]
    failing_id = ids[failure_index]
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE, fail_execute_for=frozenset({failing_id}))
    engine = _engine(step)
    manifest = _chain_manifest(ids)

    with pytest.raises(StackApplyError):
        engine.apply(manifest)

    executed_before_failure = ids[:failure_index]
    expected_calls = [("execute", rid) for rid in ids[: failure_index + 1]] + [
        ("compensate", rid) for rid in reversed(executed_before_failure)
    ]
    assert step.calls == expected_calls
    # Nothing this run created is still "live" in the fake -- full cleanup.
    assert step.live == set()


def test_chaos_rolled_back_state_is_persisted() -> None:
    ids = ["res-0", "res-1", "res-2"]
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE, fail_execute_for=frozenset({"res-2"}))
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(ids, name="rollback-stack")

    with pytest.raises(StackApplyError):
        engine.apply(manifest)

    state = repository.get("stack:rollback-stack")
    assert state is not None
    assert state.status is StackStatus.ROLLED_BACK
    by_id = {r.logical_id: r for r in state.resources}
    assert by_id["res-0"].status is ResourceStatus.COMPENSATED
    assert by_id["res-1"].status is ResourceStatus.COMPENSATED
    assert by_id["res-2"].status is ResourceStatus.FAILED


# -- the created_by_stack rule --------------------------------------------------------------


def test_reused_resource_is_never_compensated() -> None:
    ids = ["res-0", "res-1", "res-2"]
    step = FakeStackStep(
        kind=ResourceKind.IAM_ROLE,
        reused_ids=frozenset({"res-1"}),
        fail_execute_for=frozenset({"res-2"}),
    )
    engine = _engine(step)
    manifest = _chain_manifest(ids)

    with pytest.raises(StackApplyError) as exc_info:
        engine.apply(manifest)

    compensate_calls = [logical_id for verb, logical_id in step.calls if verb == "compensate"]
    assert compensate_calls == ["res-0"]  # res-1 (reused) is skipped entirely
    assert exc_info.value.orphaned == []  # nothing created_by_stack=True was left behind


# -- compensation failure --------------------------------------------------------------------


def test_compensation_failure_continues_rollback_and_preserves_the_original_cause() -> None:
    ids = ["res-0", "res-1", "res-2"]
    step = FakeStackStep(
        kind=ResourceKind.IAM_ROLE,
        fail_execute_for=frozenset({"res-2"}),
        fail_compensate_for=frozenset({"res-1"}),
    )
    engine = _engine(step)
    manifest = _chain_manifest(ids)

    with pytest.raises(StackApplyError) as exc_info:
        engine.apply(manifest)

    # Rollback still reached res-0 despite res-1's compensation failing.
    compensate_calls = [logical_id for verb, logical_id in step.calls if verb == "compensate"]
    assert compensate_calls == ["res-1", "res-0"]

    error = exc_info.value
    assert isinstance(error.__cause__, ValidationError)
    assert "res-2" in str(error.__cause__)  # the ORIGINAL apply failure, not the cleanup one

    orphaned_ids = [r.logical_id for r in error.orphaned]
    assert orphaned_ids == ["res-1"]
    assert error.orphaned[0].physical_id == "physical-res-1"


def test_compensation_failure_marks_stack_rollback_incomplete() -> None:
    ids = ["res-0", "res-1"]
    step = FakeStackStep(
        kind=ResourceKind.IAM_ROLE,
        fail_execute_for=frozenset({"res-1"}),
        fail_compensate_for=frozenset({"res-0"}),
    )
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(ids, name="incomplete-stack")

    with pytest.raises(StackApplyError):
        engine.apply(manifest)

    state = repository.get("stack:incomplete-stack")
    assert state is not None
    assert state.status is StackStatus.ROLLBACK_INCOMPLETE


# -- no_rollback ------------------------------------------------------------------------------


def test_no_rollback_leaves_created_resources_and_logs_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ids = ["res-0", "res-1"]
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE, fail_execute_for=frozenset({"res-1"}))
    engine = _engine(step)
    manifest = _chain_manifest(ids)

    with (
        caplog.at_level(logging.WARNING, logger="test.stack_engine"),
        pytest.raises(StackApplyError) as exc_info,
    ):
        engine.apply(manifest, no_rollback=True)

    compensate_calls = [call for call in step.calls if call[0] == "compensate"]
    assert compensate_calls == []
    assert step.live == {"res-0"}  # never compensated
    assert [r.logical_id for r in exc_info.value.orphaned] == ["res-0"]
    assert any("no_rollback" in record.message for record in caplog.records)


# -- dry_run ------------------------------------------------------------------------------


def test_dry_run_executes_nothing_and_writes_nothing() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(["res-a", "res-b"], name="dry-run-stack")

    state = engine.apply(manifest, dry_run=True)

    assert step.calls == []
    assert repository.get("stack:dry-run-stack") is None
    assert state.status is StackStatus.PLANNED
    assert all(r.status is ResourceStatus.PENDING for r in state.resources)


# -- plan ---------------------------------------------------------------------------------


def test_plan_marks_everything_create_on_a_fresh_stack() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    engine = _engine(step)
    manifest = _chain_manifest(["res-a", "res-b"])

    plan = engine.plan(manifest)

    assert [r.action.value for r in plan.resources] == ["create", "create"]
    assert plan.has_changes


def test_plan_marks_unchanged_resources_no_op_after_apply() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(["res-a", "res-b"])

    engine.apply(manifest)
    plan = engine.plan(manifest)

    assert [r.action.value for r in plan.resources] == ["no-op", "no-op"]
    assert not plan.has_changes


def test_plan_marks_a_changed_resource_replace() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(["res-a", "res-b"])
    engine.apply(manifest)

    changed_manifest = StackManifest(
        apiVersion="v1",
        name="chaos-stack",
        resources=[
            _resource("res-a", properties={"changed": True}),
            _resource("res-b", depends_on=["res-a"]),
        ],
    )
    plan = engine.plan(changed_manifest)

    actions = {r.logical_id: r.action.value for r in plan.resources}
    assert actions["res-a"] == "replace"
    assert actions["res-b"] == "no-op"


def test_a_failed_replace_never_reports_untouched_no_op_resources_as_orphaned() -> None:
    """Regression test: a later REPLACE failure must not sweep up unrelated, healthy,
    already-applied (NO-OP this run) resources into `.orphaned` -- they were never
    touched by this apply and are not this apply's problem to report or roll back.
    """
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(["res-a", "res-b"], name="replace-stack")
    engine.apply(manifest)  # res-a and res-b both CREATED, healthy
    step.calls.clear()

    changed_manifest = StackManifest(
        apiVersion="v1",
        name="replace-stack",
        resources=[
            _resource("res-a"),  # unchanged -> NO-OP, never touched this run
            _resource(
                "res-b", properties={"changed": True}, depends_on=["res-a"]
            ),  # changed -> REPLACE, and this run fails it
        ],
    )
    step.fail_execute_for = frozenset({"res-b"})

    with pytest.raises(StackApplyError) as exc_info:
        engine.apply(changed_manifest)

    # res-a was never executed -- it's NO-OP, correctly skipped.
    assert ("execute", "res-a") not in step.calls
    assert ("compensate", "res-a") not in step.calls
    # Nothing is orphaned: res-b never actually created anything (execute raised before
    # returning a StepResult), and res-a was never touched by this failed apply at all.
    assert exc_info.value.orphaned == []

    state = repository.get("stack:replace-stack")
    assert state is not None
    by_id = {r.logical_id: r for r in state.resources}
    # res-a's prior, healthy state must survive completely untouched.
    assert by_id["res-a"].status is ResourceStatus.CREATED
    assert by_id["res-a"].physical_id == "physical-res-a"
    # res-b's FAILED entry keeps pointing at the still-alive OLD resource, not a blank slate.
    assert by_id["res-b"].status is ResourceStatus.FAILED
    assert by_id["res-b"].physical_id == "physical-res-b"


# -- destroy --------------------------------------------------------------------------------


def test_destroy_compensates_in_reverse_order_only_created_by_stack() -> None:
    ids = ["res-0", "res-1", "res-2"]
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE, reused_ids=frozenset({"res-1"}))
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(ids, name="destroy-stack")
    engine.apply(manifest)
    step.calls.clear()

    final_state = engine.destroy("destroy-stack")

    compensate_calls = [logical_id for verb, logical_id in step.calls if verb == "compensate"]
    assert compensate_calls == ["res-2", "res-0"]  # reverse order, res-1 (reused) skipped
    assert final_state.status is StackStatus.DESTROYED
    by_id = {r.logical_id: r for r in final_state.resources}
    assert by_id["res-0"].status is ResourceStatus.DESTROYED
    assert by_id["res-1"].status is ResourceStatus.CREATED  # untouched
    assert by_id["res-2"].status is ResourceStatus.DESTROYED


def test_destroy_treats_an_already_gone_resource_as_success() -> None:
    ids = ["res-0", "res-1"]
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(ids, name="gone-stack")
    engine.apply(manifest)
    step.not_found_compensate_for = frozenset({"res-1"})

    final_state = engine.destroy("gone-stack")

    assert final_state.status is StackStatus.DESTROYED
    by_id = {r.logical_id: r for r in final_state.resources}
    assert by_id["res-1"].status is ResourceStatus.DESTROYED


def test_destroy_unknown_stack_raises_stack_not_found_error() -> None:
    engine = _engine(FakeStackStep(kind=ResourceKind.IAM_ROLE))

    with pytest.raises(StackNotFoundError):
        engine.destroy("no-existe-este-stack")


def test_destroy_without_force_stops_at_the_first_hard_failure() -> None:
    ids = ["res-0", "res-1", "res-2"]
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE, fail_compensate_for=frozenset({"res-1"}))
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(ids, name="halt-stack")
    engine.apply(manifest)
    step.calls.clear()

    final_state = engine.destroy("halt-stack", force=False)

    compensate_calls = [logical_id for verb, logical_id in step.calls if verb == "compensate"]
    assert compensate_calls == ["res-2", "res-1"]  # stopped before reaching res-0
    assert final_state.status is StackStatus.DESTROYING


# -- status / drift detection ----------------------------------------------------------------


def test_status_without_refresh_returns_persisted_state_untouched() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(["res-a"], name="status-stack")
    engine.apply(manifest)

    state = engine.status("status-stack")

    assert state.status is StackStatus.APPLIED


def test_status_unknown_stack_raises_stack_not_found_error() -> None:
    engine = _engine(FakeStackStep(kind=ResourceKind.IAM_ROLE))

    with pytest.raises(StackNotFoundError):
        engine.status("no-existe-este-stack")


def test_status_refresh_detects_drift() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(["res-a", "res-b"], name="drift-stack")
    engine.apply(manifest)
    step.live.discard("res-a")  # simulate someone deleting it outside the stack

    state = engine.status("drift-stack", refresh=True)

    by_id = {r.logical_id: r for r in state.resources}
    assert by_id["res-a"].status is ResourceStatus.FAILED
    assert by_id["res-a"].error is not None
    assert by_id["res-b"].status is ResourceStatus.CREATED

    persisted = repository.get("stack:drift-stack")
    assert persisted is not None
    persisted_res_a = persisted.get_resource("res-a")
    assert persisted_res_a is not None
    assert persisted_res_a.status is ResourceStatus.FAILED


def test_status_refresh_with_no_drift_does_not_rewrite_the_repository() -> None:
    step = FakeStackStep(kind=ResourceKind.IAM_ROLE)
    repository = InMemoryStackRepository()
    engine = _engine(step, repository)
    manifest = _chain_manifest(["res-a"], name="clean-stack")
    engine.apply(manifest)
    original = repository.get("stack:clean-stack")

    refreshed = engine.status("clean-stack", refresh=True)

    assert refreshed == original


# -- registry: every ResourceKind has a step --------------------------------------------------


def test_registry_covers_every_resource_kind() -> None:
    from pathlib import Path

    from aws_admin_cli.application.stacks.registry import StepDependencies, build_steps

    from tests.fakes.ec2 import FakeEc2Gateway
    from tests.fakes.iam import FakeIamGateway, InMemoryRepository
    from tests.fakes.s3 import FakeS3Gateway
    from tests.fakes.vpc import FakeVpcGateway

    deps = StepDependencies(
        iam_gateway=FakeIamGateway(),
        s3_gateway=FakeS3Gateway(),
        ec2_gateway=FakeEc2Gateway(),
        vpc_gateway=FakeVpcGateway(),
        repository=InMemoryRepository(),
        profile=_PROFILE,
        region=_REGION,
        logger=logging.getLogger("test.registry"),
        default_key_pair_dir=Path("/tmp"),
    )

    steps = build_steps(deps)

    for kind in ResourceKind:
        assert kind in steps, f"Falta un StackStep registrado para {kind!r}."
