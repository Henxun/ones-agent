from __future__ import annotations

import json
import multiprocessing
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.developer_workflow.contracts import (
    CodexResult,
    DefectAction,
    DefectCheckpoint,
    PublicationResult,
    RootCauseEvidence,
    RootCauseSupportingPoint,
    StateEvent,
    WorkflowRun,
    WorkflowState,
    utc_now,
)
from src.developer_workflow.state_store import (
    ConcurrentRunUpdateError,
    FileRunStore,
    InvalidRunMutationError,
    InvalidRunTransitionError,
    RunAlreadyExistsError,
    RunCorruptedError,
    RunLockTimeoutError,
    RunNotFoundError,
    UnsafeRunPathError,
)


def _hold_lock_then_crash(root: str, run_id: str, ready: object) -> None:
    store = FileRunStore(Path(root), lock_timeout=1.0, lock_poll_interval=0.01)
    with store._locked(run_id):
        ready.set()  # type: ignore[attr-defined]
        os._exit(0)


def _hold_operation_then_crash(root: str, run_id: str, ready: object) -> None:
    store = FileRunStore(Path(root), lock_timeout=1.0, lock_poll_interval=0.01)
    with store.operation_lock(run_id, "publish"):
        ready.set()  # type: ignore[attr-defined]
        os._exit(0)


def new_run(run_id: str = "a" * 32) -> WorkflowRun:
    return WorkflowRun.new("requirement", "REQ-1").validated_update(run_id=run_id)


def publication(*, partial: bool = False) -> PublicationResult:
    return PublicationResult(
        approved_fingerprint="f" * 64,
        repo_url="git@github.example:Team/Repo.git",
        provider="github",
        provider_host="github.example",
        expected_parent="a" * 40,
        expected_tree="b" * 40,
        commit_message="feat: approved",
        commit_hash="c" * 40,
        remote_branch="feature/run",
        push_completed_at=datetime(2026, 8, 10, tzinfo=UTC),
        pr_marker="ones-dev-run:abc",
        pr_base="main",
        pr_head="feature/run",
        pr_title="Approved title",
        pr_body="Approved body",
        pr_url="https://github.example/Team/Repo/pull/1",
        comment_marker="<!-- ones-dev-run:abc -->",
        comment_id="" if partial else "comment-1",
        error="comment failed" if partial else "",
    )


def test_create_load_round_trip_and_utc_version(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    created = store.create(new_run())

    loaded = store.load(created.run_id)

    assert created.version == loaded.version == 1
    assert loaded == created
    assert loaded.updated_at.tzinfo is not None
    assert loaded.updated_at.utcoffset() == UTC.utcoffset(loaded.updated_at)
    assert json.loads((tmp_path / created.run_id / "run.json").read_text("utf-8"))["run_id"] == created.run_id


@pytest.mark.skipif(sys.platform != "darwin", reason="F_GETPATH is Darwin-only")
def test_read_only_load_uses_darwin_open_descriptor_path(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    created = store.create(new_run())

    assert store.load(created.run_id, read_only=True) == created


def test_delete_removes_task_record_and_private_run_data(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    created = store.create(new_run())
    run_dir = tmp_path / created.run_id
    (run_dir / "codex-activity.jsonl").write_text("activity", encoding="utf-8")

    store.delete(created.run_id)

    assert created.run_id not in store.list_run_ids()
    assert not run_dir.exists()
    with pytest.raises(RunNotFoundError):
        store.load(created.run_id, read_only=True)


def test_delete_rejects_missing_or_unsafe_task_id(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)

    with pytest.raises(RunNotFoundError):
        store.delete("a" * 32)
    with pytest.raises(UnsafeRunPathError):
        store.delete("../escape")


def test_read_only_defect_analysis_may_complete_without_publication(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = WorkflowRun.new_defect("project", "iteration", "user", "defect").validated_update(
        run_id="b" * 32,
        defect_action=DefectAction.ANALYZE,
    )
    run = store.create(run)
    with pytest.raises(InvalidRunMutationError, match="defect_action"):
        store.save(
            run.validated_update(
                defect_action=DefectAction.ANALYZE_AND_REPAIR
            ),
            expected_version=run.version,
        )
    for target in (
        WorkflowState.READING_ONES,
        WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO,
        WorkflowState.IMPLEMENTING,
    ):
        run = store.transition(run.run_id, run.version, target, "analysis setup")
    evidence = RootCauseEvidence(
        file_path="src/view.py",
        location="render symbol",
        symbol="render",
        mechanism="Null state is dereferenced before the guard.",
        code_excerpt="return state.value",
        reproduction_test="tests/test_view.py",
        test_selector="tests/test_view.py::test_null_state",
        reproduction_command="pytest tests/test_view.py::test_null_state",
        confidence=0.9,
        insufficient_evidence=False,
        impacted_files=("src/view.py",),
        fix_steps=("Guard null state before dereferencing it.",),
        supporting_points=(
            RootCauseSupportingPoint(
                kind="code",
                description="The dereference is observable in the repository.",
                source="repository",
                file_path="src/view.py",
                snippet="return state.value",
                direct_root_cause=True,
            ),
        ),
    )
    run = store.save(
        run.validated_update(
            defect_checkpoint=DefectCheckpoint.ROOT_VERIFIED,
            root_cause_evidence=(evidence,),
            codex_results=(
                CodexResult(
                    summary="root cause",
                    root_cause_evidence=(evidence,),
                    unrelated_changes_checked=True,
                ),
            ),
        ),
        expected_version=run.version,
    )

    completed = store.transition(
        run.run_id,
        run.version,
        WorkflowState.COMPLETED,
        "complete read-only defect analysis",
    )

    assert completed.state is WorkflowState.COMPLETED
    assert store.load(completed.run_id) == completed

    regenerated = store.decide_completed_analysis(
        completed.run_id,
        completed.version,
        accept=False,
    )
    assert regenerated.state is WorkflowState.IMPLEMENTING
    assert regenerated.analysis_generation == 1
    assert regenerated.previous_analysis_results == completed.codex_results
    assert regenerated.codex_results == ()
    assert regenerated.root_cause_evidence == ()

    regenerated = store.save(
        regenerated.validated_update(
            defect_checkpoint=DefectCheckpoint.ROOT_VERIFIED,
            root_cause_evidence=(evidence,),
            codex_results=completed.codex_results,
        ),
        expected_version=regenerated.version,
    )
    regenerated = store.transition(
        regenerated.run_id,
        regenerated.version,
        WorkflowState.COMPLETED,
        "complete regenerated read-only defect analysis",
    )
    accepted = store.decide_completed_analysis(
        regenerated.run_id,
        regenerated.version,
        accept=True,
    )
    assert accepted.state is WorkflowState.IMPLEMENTING
    assert accepted.defect_action is DefectAction.ANALYZE_AND_REPAIR
    assert accepted.analysis_solution_accepted is True
    assert accepted.root_cause_evidence == (evidence,)
    assert store.load(accepted.run_id) == accepted


def test_create_rejects_wrong_initial_state_or_version(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    with pytest.raises(InvalidRunMutationError, match="CREATED.*version 0"):
        store.create(new_run().validated_update(version=1))
    with pytest.raises(InvalidRunMutationError, match="CREATED.*version 0"):
        store.create(new_run("b" * 32).validated_update(state=WorkflowState.READING_ONES))
    with pytest.raises(InvalidRunMutationError, match="retry_count"):
        store.create(new_run("c" * 32).validated_update(retry_count=-1))


def test_create_rejects_version_zero_run_with_prepopulated_history(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path)
    now = utc_now()
    run = new_run().validated_update(
        history=(
            StateEvent(
                source=WorkflowState.CREATED,
                target=WorkflowState.BLOCKED,
                reason="block",
                occurred_at=now,
            ),
            StateEvent(
                source=WorkflowState.BLOCKED,
                target=WorkflowState.CREATED,
                reason="resume",
                occurred_at=now,
            ),
        )
    )
    with pytest.raises(InvalidRunMutationError, match="version"):
        store.create(run)


def test_load_rejects_version_below_history_floor_but_save_may_exceed_it(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path)
    created = store.create(new_run())
    reading = store.transition(
        created.run_id, created.version, WorkflowState.READING_ONES, "read"
    )
    run_file = tmp_path / reading.run_id / "run.json"
    payload = json.loads(run_file.read_text("utf-8"))
    payload["version"] = 1
    run_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RunCorruptedError) as error:
        store.load(reading.run_id)
    assert error.value.__cause__ is None

    run_file.write_text(json.dumps({**payload, "version": 2}), encoding="utf-8")
    saved = store.save(store.load(reading.run_id), 2)
    saved = store.save(saved, 3)
    assert saved.version == 4
    assert len(saved.history) == 1


def test_duplicate_missing_and_corrupt_are_safe_errors(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = new_run()
    store.create(run)
    with pytest.raises(RunAlreadyExistsError):
        store.create(run)
    with pytest.raises(RunNotFoundError):
        store.load("b" * 32)

    run_file = tmp_path / run.run_id / "run.json"
    valid_payload = run_file.read_text("utf-8")
    for corrupt in (
        "not-json SECRET",
        json.dumps({"run_id": run.run_id, "unknown": "SECRET"}),
        valid_payload.replace('Z"', '"'),
    ):
        run_file.write_text(corrupt, encoding="utf-8")
        with pytest.raises(RunCorruptedError) as error:
            store.load(run.run_id)
        assert str(error.value) == "stored workflow run is corrupted"
        assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("run_id", ["../escape", "A" * 32, "a" * 31, "g" * 32])
def test_run_id_rejects_path_traversal(run_id: str, tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    with pytest.raises(UnsafeRunPathError):
        store.load(run_id)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / ("a" * 32)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(UnsafeRunPathError):
        FileRunStore(root).load("a" * 32)


def test_run_root_symlink_or_reparse_point_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside-root"
    outside.mkdir()
    link = tmp_path / "linked-root"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(UnsafeRunPathError):
        FileRunStore(link)


def test_save_uses_cas_and_preserves_history_prefix(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    created = store.create(new_run())
    reading = store.transition(created.run_id, 1, WorkflowState.READING_ONES, "read")
    with pytest.raises(ConcurrentRunUpdateError):
        store.save(reading, expected_version=1)

    rewritten = reading.validated_update(history=())
    with pytest.raises(InvalidRunMutationError, match="history"):
        store.save(rewritten, expected_version=2)
    assert store.load(created.run_id) == reading


def test_public_save_cannot_mutate_store_owned_state_or_block_metadata(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path)
    created = store.create(new_run())
    blocked = store.transition(
        created.run_id, created.version, WorkflowState.BLOCKED, "blocked"
    )

    mutations = (
        {"state": WorkflowState.CREATED},
        {"history": ()},
        {"resume_state": WorkflowState.COMPLETED},
        {"blocked_reason": "poisoned"},
        {"resume_state": WorkflowState.PUBLISHING},
    )
    for update in mutations:
        with pytest.raises(InvalidRunMutationError):
            store.save(blocked.validated_update(**update), blocked.version)

    with pytest.raises(InvalidRunTransitionError):
        store.transition(
            blocked.run_id, blocked.version, WorkflowState.PUBLISHING, "skip"
        )
    resumed = store.transition(
        blocked.run_id, blocked.version, WorkflowState.CREATED, "resume safely"
    )
    assert resumed.state is WorkflowState.CREATED
    assert resumed.resume_state is None
    assert resumed.blocked_reason == ""


def test_publication_intent_and_effect_facts_are_immutable(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    current = store.create(new_run())
    for target in (
        WorkflowState.READING_ONES, WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO, WorkflowState.IMPLEMENTING,
        WorkflowState.TESTING, WorkflowState.AI_REVIEW,
        WorkflowState.WAITING_APPROVAL, WorkflowState.PUBLISHING,
    ):
        current = store.transition(current.run_id, current.version, target, "advance")
    current = store.save(
        current.validated_update(publication=publication(partial=True)), current.version
    )
    for field, value in (("pr_title", "poisoned"), ("commit_hash", "d" * 40)):
        changed = current.publication.model_copy(update={field: value})
        with pytest.raises(InvalidRunMutationError, match="publication"):
            store.save(current.validated_update(publication=changed), current.version)


def test_terminal_publication_states_require_strict_facts_on_transition_and_load(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path)
    current = store.create(new_run())
    for target in (
        WorkflowState.READING_ONES, WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO, WorkflowState.IMPLEMENTING,
        WorkflowState.TESTING, WorkflowState.AI_REVIEW,
        WorkflowState.WAITING_APPROVAL, WorkflowState.PUBLISHING,
    ):
        current = store.transition(current.run_id, current.version, target, "advance")
    with pytest.raises(InvalidRunMutationError, match="completed publication"):
        store.transition(current.run_id, current.version, WorkflowState.COMPLETED, "invalid")
    with pytest.raises(InvalidRunMutationError, match="partial publication"):
        store.transition(current.run_id, current.version, WorkflowState.PARTIAL_SUCCESS, "invalid")

    completed = store.save(
        current.validated_update(publication=publication()), current.version
    )
    completed = store.transition(
        completed.run_id, completed.version, WorkflowState.COMPLETED, "complete"
    )
    run_file = tmp_path / completed.run_id / "run.json"
    payload = json.loads(run_file.read_text("utf-8"))
    payload["publication"]["comment_id"] = ""
    run_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RunCorruptedError):
        store.load(completed.run_id)


def test_semantically_corrupt_runs_raise_one_safe_error(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    created = store.create(new_run())
    run_file = tmp_path / created.run_id / "run.json"
    valid = json.loads(run_file.read_text("utf-8"))
    occurred_at = valid["updated_at"]

    def event(source: str, target: str, reason: str = "advance") -> dict[str, str]:
        return {
            "source": source,
            "target": target,
            "reason": reason,
            "occurred_at": occurred_at,
        }

    corrupt_payloads = (
        {**valid, "version": -1},
        {**valid, "state": "COMPLETED", "history": []},
        {
            **valid,
            "state": "VALIDATING",
            "history": [
                event("CREATED", "READING_ONES"),
                event("CREATED", "VALIDATING"),
            ],
        },
        {
            **valid,
            "state": "IMPLEMENTING",
            "history": [event("CREATED", "IMPLEMENTING")],
        },
        {
            **valid,
            "state": "CREATED",
            "history": [event("CREATED", "READING_ONES")],
        },
        {
            **valid,
            "state": "BLOCKED",
            "history": [event("CREATED", "BLOCKED")],
            "resume_state": "PUBLISHING",
            "blocked_reason": "blocked",
        },
        {
            **valid,
            "state": "BLOCKED",
            "history": [event("CREATED", "BLOCKED", "")],
            "resume_state": "CREATED",
            "blocked_reason": "",
        },
        {
            **valid,
            "state": "BLOCKED",
            "history": [event("CREATED", "BLOCKED", "event reason")],
            "resume_state": "CREATED",
            "blocked_reason": "different reason",
        },
        {
            **valid,
            "state": "READING_ONES",
            "history": [event("CREATED", "READING_ONES")],
            "resume_state": "CREATED",
            "blocked_reason": "stale",
        },
    )
    for payload in corrupt_payloads:
        run_file.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(RunCorruptedError) as error:
            store.load(created.run_id)
        assert str(error.value) == "stored workflow run is corrupted"
        assert error.value.__cause__ is None


def test_save_does_not_redirect_an_existing_run_to_another_id(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    created = store.create(new_run())
    with pytest.raises(RunNotFoundError):
        store.save(created.validated_update(run_id="b" * 32), expected_version=1)
    assert store.load(created.run_id) == created


def test_atomic_replace_uses_unique_temps_and_cleans_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    created = store.create(new_run())
    original = (tmp_path / created.run_id / "run.json").read_bytes()
    replace_sources: list[Path] = []
    real_replace = os.replace

    def capture_replace(source: str | Path, target: str | Path) -> None:
        replace_sources.append(Path(source))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", capture_replace)
    saved = store.save(created, 1)
    store.save(saved, 2)
    assert len({path.name for path in replace_sources}) == 2
    assert all(path.parent == tmp_path / created.run_id for path in replace_sources)

    def fail_replace(source: str | Path, target: str | Path) -> None:
        raise OSError("replace failed")

    before_failure = (tmp_path / created.run_id / "run.json").read_bytes()
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.save(store.load(created.run_id), 3)
    assert (tmp_path / created.run_id / "run.json").read_bytes() == before_failure
    assert not list((tmp_path / created.run_id).glob(".run-*.tmp"))
    assert before_failure != original


def test_full_main_chain_appends_history_and_versions(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    current = store.create(new_run())
    chain = [
        WorkflowState.READING_ONES,
        WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO,
        WorkflowState.IMPLEMENTING,
        WorkflowState.TESTING,
        WorkflowState.AI_REVIEW,
        WorkflowState.WAITING_APPROVAL,
        WorkflowState.PUBLISHING,
        WorkflowState.COMPLETED,
    ]
    for target in chain:
        previous = current
        if target is WorkflowState.COMPLETED:
            current = store.save(
                current.validated_update(publication=publication()), current.version
            )
            previous = current
        current = store.transition(current.run_id, current.version, target, target.value)
        assert current.version == previous.version + 1
        assert current.history[:-1] == previous.history
        assert current.history[-1].source is previous.state
        assert current.history[-1].target is target
        assert current.history[-1].occurred_at.tzinfo is not None
    assert current.state is WorkflowState.COMPLETED


def test_illegal_jump_terminal_and_empty_reason(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = store.create(new_run())
    with pytest.raises(InvalidRunTransitionError):
        store.transition(run.run_id, 1, WorkflowState.IMPLEMENTING, "jump")
    with pytest.raises(ValueError, match="reason"):
        store.transition(run.run_id, 1, WorkflowState.READING_ONES, "  ")
    cancelled = store.transition(run.run_id, 1, WorkflowState.CANCELLED, "stop")
    with pytest.raises(InvalidRunTransitionError):
        store.transition(cancelled.run_id, 2, WorkflowState.READING_ONES, "restart")


def test_blocked_resume_only_saved_safe_state_and_clears_metadata(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    current = store.create(new_run())
    current = store.transition(current.run_id, 1, WorkflowState.READING_ONES, "read")
    blocked = store.transition(current.run_id, 2, WorkflowState.BLOCKED, "waiting")
    assert blocked.resume_state is WorkflowState.READING_ONES
    assert blocked.blocked_reason == "waiting"
    with pytest.raises(InvalidRunTransitionError):
        store.transition(blocked.run_id, 3, WorkflowState.VALIDATING, "wrong resume")
    resumed = store.transition(blocked.run_id, 3, WorkflowState.READING_ONES, "resume")
    assert resumed.resume_state is None
    assert resumed.blocked_reason == ""

    with pytest.raises(InvalidRunTransitionError):
        store.transition(resumed.run_id, 4, WorkflowState.BLOCKED, "skip", WorkflowState.IMPLEMENTING)


def test_partial_success_can_only_retry_publishing(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    current = store.create(new_run())
    for target in (
        WorkflowState.READING_ONES, WorkflowState.VALIDATING, WorkflowState.PREPARING_REPO,
        WorkflowState.IMPLEMENTING, WorkflowState.TESTING, WorkflowState.AI_REVIEW,
        WorkflowState.WAITING_APPROVAL, WorkflowState.PUBLISHING,
    ):
        current = store.transition(current.run_id, current.version, target, "advance")
    current = store.save(
        current.validated_update(publication=publication(partial=True)), current.version
    )
    partial = store.transition(current.run_id, current.version, WorkflowState.PARTIAL_SUCCESS, "push failed")
    with pytest.raises(InvalidRunTransitionError):
        store.transition(partial.run_id, partial.version, WorkflowState.COMPLETED, "wrong")
    publishing = store.transition(partial.run_id, partial.version, WorkflowState.PUBLISHING, "retry")
    assert publishing.state is WorkflowState.PUBLISHING


def test_cancel_is_allowed_from_blocked_and_partial_success(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    blocked = store.create(new_run())
    blocked = store.transition(blocked.run_id, blocked.version, WorkflowState.BLOCKED, "wait")
    assert store.transition(
        blocked.run_id, blocked.version, WorkflowState.CANCELLED, "cancel blocked"
    ).state is WorkflowState.CANCELLED

    partial = store.create(new_run("b" * 32))
    for target in (
        WorkflowState.READING_ONES, WorkflowState.VALIDATING, WorkflowState.PREPARING_REPO,
        WorkflowState.IMPLEMENTING, WorkflowState.TESTING, WorkflowState.AI_REVIEW,
        WorkflowState.WAITING_APPROVAL, WorkflowState.PUBLISHING, WorkflowState.PARTIAL_SUCCESS,
    ):
        if target is WorkflowState.PARTIAL_SUCCESS:
            partial = store.save(
                partial.validated_update(publication=publication(partial=True)),
                partial.version,
            )
        partial = store.transition(partial.run_id, partial.version, target, "advance")
    assert store.transition(
        partial.run_id, partial.version, WorkflowState.CANCELLED, "cancel partial"
    ).state is WorkflowState.CANCELLED


def test_lock_timeout_then_eventual_thread_consistency(tmp_path: Path) -> None:
    slow = FileRunStore(tmp_path, lock_timeout=0.08, lock_poll_interval=0.01)
    peer = FileRunStore(tmp_path, lock_timeout=1.0, lock_poll_interval=0.01)
    created = slow.create(new_run())
    lock_path = tmp_path / created.run_id / ".lock"
    with slow._locked(created.run_id):
        with pytest.raises(RunLockTimeoutError):
            FileRunStore(
                tmp_path, lock_timeout=0.08, lock_poll_interval=0.01
            ).save(created, 1)

    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def update() -> None:
        barrier.wait()
        try:
            outcomes.append(peer.transition(created.run_id, 1, WorkflowState.READING_ONES, "race"))
        except Exception as exc:  # asserted below
            outcomes.append(exc)

    threads = [threading.Thread(target=update) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert sum(isinstance(item, WorkflowRun) for item in outcomes) == 1
    assert sum(isinstance(item, ConcurrentRunUpdateError) for item in outcomes) == 1
    assert peer.load(created.run_id).version == 2
    assert lock_path.exists()


def test_process_crash_releases_advisory_lock(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path, lock_timeout=1.0, lock_poll_interval=0.01)
    created = store.create(new_run())
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=_hold_lock_then_crash,
        args=(str(tmp_path), created.run_id, ready),
    )
    process.start()
    assert ready.wait(5)
    process.join(5)
    assert process.exitcode == 0

    saved = store.save(created, created.version)
    assert saved.version == 2
    assert (tmp_path / created.run_id / ".lock").exists()


def test_run_directory_identity_change_while_waiting_for_lock_fails_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = FileRunStore(tmp_path, lock_timeout=1.0, lock_poll_interval=0.01)
    contender = FileRunStore(tmp_path, lock_timeout=1.0, lock_poll_interval=0.01)
    created = holder.create(new_run())
    outside = tmp_path / "outside"
    outside.mkdir()
    initial_checked = threading.Event()
    calls = 0
    real_validate = contender._validate_run_identity

    def simulate_identity_replacement(
        run_id: str, expected: tuple[int, int] | None = None
    ) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            identity = real_validate(run_id, expected)
            initial_checked.set()
            return identity
        contender._run_identities[run_id] = (-1, -1)
        return real_validate(run_id, expected)

    monkeypatch.setattr(contender, "_validate_run_identity", simulate_identity_replacement)
    outcomes: list[Exception] = []

    def save_after_wait() -> None:
        try:
            contender.save(created, created.version)
        except Exception as exc:
            outcomes.append(exc)

    with holder._locked(created.run_id):
        thread = threading.Thread(target=save_after_wait)
        thread.start()
        assert initial_checked.wait(2)
        time.sleep(0.03)
    thread.join(2)

    assert len(outcomes) == 1
    assert isinstance(outcomes[0], UnsafeRunPathError)
    assert holder.load(created.run_id) == created
    assert not (outside / "run.json").exists()


@pytest.mark.parametrize(
    "terminal",
    [WorkflowState.CANCELLED, WorkflowState.FAILED, WorkflowState.COMPLETED],
)
def test_all_terminal_states_have_no_outgoing_edges(
    terminal: WorkflowState, tmp_path: Path
) -> None:
    store = FileRunStore(tmp_path)
    current = store.create(new_run())
    if terminal is WorkflowState.COMPLETED:
        for target in (
            WorkflowState.READING_ONES,
            WorkflowState.VALIDATING,
            WorkflowState.PREPARING_REPO,
            WorkflowState.IMPLEMENTING,
            WorkflowState.TESTING,
            WorkflowState.AI_REVIEW,
            WorkflowState.WAITING_APPROVAL,
            WorkflowState.PUBLISHING,
            WorkflowState.COMPLETED,
        ):
            if target is WorkflowState.COMPLETED:
                current = store.save(
                    current.validated_update(publication=publication()), current.version
                )
            current = store.transition(current.run_id, current.version, target, "advance")
    else:
        current = store.transition(current.run_id, current.version, terminal, "stop")
    with pytest.raises(InvalidRunTransitionError):
        store.transition(
            current.run_id, current.version, WorkflowState.CANCELLED, "outgoing"
        )


def test_load_never_observes_half_file_during_replaces(tmp_path: Path) -> None:
    writer = FileRunStore(tmp_path)
    reader = FileRunStore(tmp_path)
    current = writer.create(new_run())
    failures: list[Exception] = []
    stop = threading.Event()
    first_read = threading.Event()

    def read_loop() -> None:
        while not stop.is_set():
            try:
                reader.load(current.run_id)
                first_read.set()
            except Exception as exc:
                failures.append(exc)
                stop.set()
            time.sleep(0.001)

    thread = threading.Thread(target=read_loop)
    thread.start()
    assert first_read.wait(2)
    for _ in range(20):
        current = writer.save(current, current.version)
    stop.set()
    thread.join(timeout=2)
    assert failures == []
    assert reader.load(current.run_id).version == 21


def test_operation_lease_is_released_when_holder_process_crashes(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path, lock_timeout=2.0, lock_poll_interval=0.01)
    created = store.create(new_run())
    ready = multiprocessing.Event()
    process = multiprocessing.Process(
        target=_hold_operation_then_crash,
        args=(str(tmp_path), created.run_id, ready),
    )
    process.start()
    assert ready.wait(2)
    process.join(2)
    assert process.exitcode == 0
    with store.operation_lock(created.run_id, "publish"):
        assert store.load(created.run_id) == created
