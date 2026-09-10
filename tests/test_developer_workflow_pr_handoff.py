from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
import httpx
import pytest

from src.developer_workflow import pr_handoff as h, verification as v
from src.developer_workflow.approval import approval_fingerprint, issue_approval, validate_for_approval, ApprovalValidationError
from src.developer_workflow.contracts import CodexResult, WorkflowState, PreparedWorktree, RepositorySnapshot
from src.developer_workflow.pr_provider import PullRequestProviderError
from src.developer_workflow.publisher import Publisher, PublicationBlocked
from src.developer_workflow.state_store import InvalidRunMutationError, InvalidRunTransitionError
from src.developer_workflow.orchestrator import DeveloperWorkflowOrchestrator, InvalidWorkflowAction
from src.developer_workflow.tui.models import RunDetail, DangerousActionRequest
from src.developer_workflow.ones_comment import build_comment_text
from test_developer_workflow_verification import reviewed, need
from test_developer_workflow_publisher import _stored_waiting, _publisher, FakeRepository, FakeCommenter
from test_developer_workflow_pr_provider import _client
from test_developer_workflow_multi_publisher import _waiting_group, GroupRepository, GroupCommenter


def deferred(tmp_path):
    flow, store, repo, codex, old = reviewed(tmp_path)
    flow.config = flow.config.validated_update(publishing=flow.config.publishing.validated_update(
        defer_external_verification_to_pr=True))
    before = tuple(codex.stages)
    run = flow.execute(old)
    assert tuple(codex.stages) == before  # No repeated expensive AI review for missing hardware.
    assert run.state is WorkflowState.WAITING_APPROVAL, run.blocked_reason
    return flow, store, run


def test_legacy_environment_block_advances_to_draft_approval(tmp_path):
    _, _, run = deferred(tmp_path)
    assert run.approval.draft_pr
    assert run.approval.verification_records == ()
    assert run.approval.deferred_verification == run.verification_plan
    assert run.verification_plan[0].status == "waiting_environment"
    assert "- [ ]" in run.approval.pr_body and need().acceptance in run.approval.pr_body
    assert v.snapshot_digest(run) in run.approval.pr_body
    assert h.CHECK_CONTEXT in run.approval.pr_body
    h.assert_bound(run)
    request = DangerousActionRequest.from_run(run, action="approve")
    assert request.draft_pr and request.deferred_check_count == 1


@pytest.mark.parametrize("status", ["failed", "error", "running", "stale"])
def test_failed_or_in_flight_verification_cannot_be_deferred(tmp_path, status):
    _, _, run = deferred(tmp_path)
    tasks = (run.verification_plan[0].model_copy(update={"status": status}),)
    assert h.blocking_reason(tasks, defer=True)
    with pytest.raises((ValueError, ApprovalValidationError)):
        validate_for_approval(run.approval.model_copy(update={"deferred_verification": tasks}))


@pytest.mark.parametrize("change", ["snapshot", "missing", "acceptance", "dropped", "false_pass"])
def test_handoff_is_bound_to_exact_review_and_snapshot(tmp_path, change):
    _, _, run = deferred(tmp_path)
    if change == "snapshot":
        run = run.validated_update(tested_snapshot=run.tested_snapshot.validated_update(diff_sha256="f" * 64))
    elif change == "missing":
        run = run.validated_update(verification_plan=())
    elif change == "acceptance":
        run = run.validated_update(review=run.review.validated_update(
            verification_needs=(need().model_copy(update={"acceptance": "new criterion"}),)))
    elif change == "dropped":
        run = run.validated_update(approval=run.approval.validated_update(deferred_verification=()))
    else:
        run = run.validated_update(verification_plan=(run.verification_plan[0].model_copy(update={"status": "passed"}),))
    with pytest.raises((ValueError, StopIteration)):
        h.assert_bound(run)


def test_changed_pending_check_invalidates_approval_fingerprint(tmp_path):
    _, _, run = deferred(tmp_path)
    package = run.approval
    changed = package.validated_update(deferred_verification=())
    assert approval_fingerprint(package) != approval_fingerprint(changed)


def test_production_rebuilder_keeps_handoff_and_rejects_stale_snapshot(tmp_path):
    from test_developer_workflow_approval_rebuilder import _run
    from src.developer_workflow.approval_rebuilder import WorkflowApprovalRebuilder, ApprovalRebuildError
    run, gateway, repository = _run(tmp_path)
    run = run.validated_update(review=CodexResult(summary="Reviewed", verification_needs=(need(),)))
    run = run.validated_update(verification_plan=v.plan(run, ()))
    run = run.validated_update(approval=h.prepare(run, run.approval))
    rebuilder = WorkflowApprovalRebuilder(gateway, repository)
    rebuilt = rebuilder.rebuild(run)
    assert rebuilt.deferred_verification == run.verification_plan
    assert rebuilt.verification_records == ()
    repository.current = repository.current.validated_update(diff_sha256="f" * 64)
    with pytest.raises(ApprovalRebuildError):
        rebuilder.rebuild(run)


def test_approval_obeys_strict_policy_even_for_existing_draft_package(tmp_path):
    from types import SimpleNamespace
    from src.developer_workflow.orchestrator import DeveloperWorkflowOrchestrator
    flow, store, run = deferred(tmp_path)
    flow.config = flow.config.validated_update(publishing=flow.config.publishing.validated_update(
        defer_external_verification_to_pr=False))
    service = DeveloperWorkflowOrchestrator(store=store, config=flow.config, defect_flow=flow,
        requirement_flow=SimpleNamespace(), defect_candidates=SimpleNamespace(),
        publisher=SimpleNamespace(publish=lambda _: pytest.fail("must not publish")))
    blocked = service.approve(run.run_id, "operator", expected_version=run.version)
    assert blocked.state is WorkflowState.BLOCKED and blocked.approval is None


def test_explicit_approval_preserves_pending_checks_when_dispatching_publisher(tmp_path):
    from types import SimpleNamespace
    from src.developer_workflow.orchestrator import DeveloperWorkflowOrchestrator
    flow, store, run = deferred(tmp_path)
    dispatched = []
    def publish(signed):
        dispatched.append(signed)
        return signed
    service = DeveloperWorkflowOrchestrator(store=store, config=flow.config, defect_flow=flow,
        requirement_flow=SimpleNamespace(), defect_candidates=SimpleNamespace(), publisher=SimpleNamespace(publish=publish))
    result = service.approve(run.run_id, "operator", expected_version=run.version)
    assert len(dispatched) == 1 and result.approval.approved_by == "operator"
    assert result.approval.draft_pr and result.approval.verification_records == ()


class DraftPR:
    def __init__(self, fail_check=False, uncertain=False):
        self.urls, self.created, self.pending = {}, [], []
        self.fail_check, self.uncertain = fail_check, uncertain

    def set_verification_pending(self, *, repo_url, sha, branch):
        if self.fail_check:
            raise RuntimeError("check unavailable")
        self.pending.append((repo_url, sha, branch))

    def find(self, *, repo_url, **kwargs):
        assert kwargs.get("draft") is True and kwargs.get("expected_sha")
        return self.urls.get(repo_url)

    def create(self, *, repo_url, body, **kwargs):
        assert kwargs["draft"] is True and kwargs["expected_sha"] in body
        self.created.append(repo_url)
        self.urls[repo_url] = repo_url.removesuffix(".git") + "/pull/1"
        if self.uncertain:
            self.uncertain = False
            raise RuntimeError("response lost after creation")
        return self.urls[repo_url]


def draft_waiting(tmp_path):
    store, run = _stored_waiting(tmp_path / "publish", signed=False)
    package = run.approval
    run = run.validated_update(repository=package.repository,
        prepared_worktree=PreparedWorktree(path=(tmp_path / "checkout").resolve(), mirror_path=(tmp_path / "mirror").resolve(),
            branch=package.branch, base_commit=package.base_commit, head_commit=package.head_commit),
        tested_snapshot=RepositorySnapshot(head_commit=package.head_commit, diff_sha256=package.diff_hash,
            changed_files=package.changed_files, patch="test patch", is_clean=False),
        branch=package.branch, base_commit=package.base_commit, head_commit=package.head_commit,
        changed_files=package.changed_files, test_results=package.tests,
        review=CodexResult(summary="Local review passed; hardware verification pending", verification_needs=(need(),)))
    run = run.validated_update(verification_plan=v.plan(run, ()))
    package = h.prepare(run, package)
    package = issue_approval(package, approved_by="operator")
    run = store.save(run.validated_update(approval=package), run.version)
    return store, run, package


@pytest.mark.parametrize("uncertain", [False, True])
def test_single_repo_draft_delivery_is_not_completion_and_is_idempotent(tmp_path, uncertain):
    store, run, package = draft_waiting(tmp_path)
    pr, repo, commenter = DraftPR(uncertain=uncertain), FakeRepository(), FakeCommenter()
    publisher = _publisher(store, run, repo=repo, pr=pr, commenter=commenter, rebuild=lambda _: package)
    delivered = publisher.publish(run)
    assert delivered.state is WorkflowState.WAITING_PR_VERIFICATION
    assert delivered.publication.draft_pr
    assert len(pr.pending) == len(pr.created) == 1
    assert pr.pending[0][1] == delivered.publication.commit_hash
    assert delivered.approval.verification_records == ()
    assert publisher.publish(delivered) == delivered
    assert len(pr.created) == 1 and commenter.calls == 1
    assert store.load(run.run_id) == delivered
    with pytest.raises(InvalidRunTransitionError):
        store.transition(run.run_id, delivered.version, WorkflowState.COMPLETED, "not allowed")
    assert "Draft PR" in build_comment_text(delivered, summary="local fix", tests_summary="passed locally")
    detail = RunDetail.from_run(delivered)
    assert detail.draft_pr and not detail.can_verify and not detail.can_request_review_repair
    assert "等待 PR 人工验证" in detail.status_message


def test_post_draft_human_verification_records_merge_readiness_only(tmp_path):
    store, run, package = draft_waiting(tmp_path)
    publisher = _publisher(
        store,
        run,
        repo=FakeRepository(),
        pr=DraftPR(),
        commenter=FakeCommenter(),
        rebuild=lambda _: package,
    )
    delivered = publisher.publish(run)
    service = DeveloperWorkflowOrchestrator(
        store=store,
        config=SimpleNamespace(),
        defect_flow=SimpleNamespace(),
        requirement_flow=SimpleNamespace(),
        defect_candidates=SimpleNamespace(),
        publisher=SimpleNamespace(publish=lambda _: pytest.fail("must not publish")),
    )

    recorded = service.record_merge_readiness(
        delivered.run_id,
        "reviewer",
        "Verified the deferred checks on the Draft PR commit.",
        expected_version=delivered.version,
    )

    assert recorded.state is WorkflowState.WAITING_PR_VERIFICATION
    assert recorded.publication == delivered.publication
    assert recorded.merge_readiness is not None
    assert recorded.merge_readiness.status == "passed"
    assert recorded.merge_readiness_history == (recorded.merge_readiness,)
    assert recorded.merge_readiness.approval_fingerprint == recorded.approval.fingerprint
    assert (
        recorded.merge_readiness.verification_digest,
        recorded.merge_readiness.publication_digest,
    ) == h.merge_readiness_binding(recorded)
    assert service.record_merge_readiness(
        recorded.run_id,
        "reviewer",
        "Verified the deferred checks on the Draft PR commit.",
        expected_version=recorded.version,
    ) == recorded
    with pytest.raises(InvalidRunTransitionError):
        store.transition(
            recorded.run_id,
            recorded.version,
            WorkflowState.COMPLETED,
            "must still wait for platform merge",
        )


def test_merge_readiness_clear_and_rerecord_are_append_only(tmp_path):
    store, waiting, package = draft_waiting(tmp_path)
    delivered = _publisher(
        store,
        waiting,
        repo=FakeRepository(),
        pr=DraftPR(),
        commenter=FakeCommenter(),
        rebuild=lambda _: package,
    ).publish(waiting)
    service = DeveloperWorkflowOrchestrator(
        store=store,
        config=SimpleNamespace(),
        defect_flow=SimpleNamespace(),
        requirement_flow=SimpleNamespace(),
        defect_candidates=SimpleNamespace(),
        publisher=SimpleNamespace(publish=lambda _: pytest.fail("must not publish")),
    )

    passed = service.record_merge_readiness(
        delivered.run_id,
        "reviewer-a",
        "Verified on the Draft PR commit.",
        expected_version=delivered.version,
    )
    queried = service.get_merge_readiness(
        passed.run_id, expected_version=passed.version
    )
    assert queried == passed
    with pytest.raises(InvalidWorkflowAction, match="workflow changed"):
        service.clear_merge_readiness(
            passed.run_id,
            "reviewer-a",
            "Evidence invalidated.",
            expected_version=delivered.version,
        )

    cleared = service.clear_merge_readiness(
        passed.run_id,
        "reviewer-b",
        "The tested artifact was replaced.",
        expected_version=passed.version,
    )
    assert cleared.state is WorkflowState.WAITING_PR_VERIFICATION
    assert cleared.publication == delivered.publication
    assert cleared.merge_readiness is None
    assert [item.status for item in cleared.merge_readiness_history] == [
        "passed",
        "cleared",
    ]
    assert {
        item.approval_fingerprint for item in cleared.merge_readiness_history
    } == {cleared.approval.fingerprint}
    verification_digest, publication_digest = h.merge_readiness_binding(cleared)
    assert {
        item.verification_digest for item in cleared.merge_readiness_history
    } == {verification_digest}
    assert {
        item.publication_digest for item in cleared.merge_readiness_history
    } == {publication_digest}
    with pytest.raises(InvalidWorkflowAction, match="active merge readiness"):
        service.clear_merge_readiness(
            cleared.run_id,
            "reviewer-b",
            "Duplicate clear.",
            expected_version=cleared.version,
        )

    rerecorded = service.record_merge_readiness(
        cleared.run_id,
        "reviewer-c",
        "Re-verified the replacement artifact.",
        expected_version=cleared.version,
    )
    assert rerecorded.state is WorkflowState.WAITING_PR_VERIFICATION
    assert rerecorded.merge_readiness is not None
    assert [item.status for item in rerecorded.merge_readiness_history] == [
        "passed",
        "cleared",
        "passed",
    ]
    with pytest.raises(InvalidRunTransitionError):
        store.transition(
            rerecorded.run_id,
            rerecorded.version,
            WorkflowState.COMPLETED,
            "readiness does not complete the run",
        )


def test_merge_readiness_rejects_wrong_state_and_is_immutable(tmp_path):
    store, waiting, _ = draft_waiting(tmp_path)
    service = DeveloperWorkflowOrchestrator(
        store=store,
        config=SimpleNamespace(),
        defect_flow=SimpleNamespace(),
        requirement_flow=SimpleNamespace(),
        defect_candidates=SimpleNamespace(),
        publisher=SimpleNamespace(),
    )
    with pytest.raises(InvalidWorkflowAction, match="WAITING_PR_VERIFICATION"):
        service.record_merge_readiness(
            waiting.run_id, "reviewer", "Checked.", expected_version=waiting.version
        )

    delivered = _publisher(
        store,
        waiting,
        repo=FakeRepository(),
        pr=DraftPR(),
        commenter=FakeCommenter(),
        rebuild=lambda _: waiting.approval,
    ).publish(waiting)
    recorded = service.record_merge_readiness(
        delivered.run_id, "reviewer", "Checked.", expected_version=delivered.version
    )
    replacement = recorded.merge_readiness.model_copy(
        update={"actor": "another reviewer"}
    )
    with pytest.raises(InvalidRunMutationError, match="immutable"):
        store.save(
            recorded.validated_update(merge_readiness=replacement), recorded.version
        )


def test_pending_status_failure_does_not_publish_ready_pr(tmp_path):
    store, run, package = draft_waiting(tmp_path)
    pr = DraftPR(fail_check=True)
    publisher = _publisher(store, run, pr=pr, rebuild=lambda _: package)
    with pytest.raises(PublicationBlocked):
        publisher.publish(run)
    assert not pr.created
    assert store.load(run.run_id).state is WorkflowState.PUBLISHING
    pr.fail_check = False
    assert publisher.publish(store.load(run.run_id)).state is WorkflowState.WAITING_PR_VERIFICATION


def test_group_draft_delivery_retains_checks_for_every_changed_repository(tmp_path):
    store, run, approval, _ = _waiting_group(tmp_path / "group")
    run = run.validated_update(review=CodexResult(summary="Local review passed", verification_needs=(need(),)))
    run = run.validated_update(verification_plan=v.plan(run, ()))
    package = issue_approval(h.prepare(run, approval), approved_by="operator")
    run = store.save(run.validated_update(approval=package), run.version)
    h.assert_bound(run)
    pr = DraftPR()
    publisher = Publisher(store, GroupRepository(), lambda _: package, pr, GroupCommenter(), "github", "github.example")
    delivered = publisher.publish(run)
    assert delivered.state is WorkflowState.WAITING_PR_VERIFICATION
    assert len(pr.created) == len(pr.pending) == 2
    assert all(item.draft_pr and "- [ ]" in item.pr_body for item in delivered.group_publication.repositories)
    assert store.load(run.run_id) == delivered


def test_requirement_default_policy_reaches_draft_approval(tmp_path):
    from test_developer_workflow_requirement import _flow
    flow, store = _flow(tmp_path)
    run = flow.execute(store.run)
    run = run.validated_update(repository=flow.config.repositories[0])
    store.run = run
    original = flow.codex.run_stage
    def stage(stage, **kwargs):
        result = original(stage, **kwargs)
        return result.validated_update(verification_needs=(need(),)) if stage == "review" else result
    flow.codex.run_stage = stage
    run = flow.execute(run)
    assert run.state is WorkflowState.WAITING_APPROVAL
    assert run.approval.draft_pr and run.approval.verification_records == ()
    h.assert_bound(run)


def test_remote_branch_change_after_push_prevents_draft_creation(tmp_path):
    store, run, package = draft_waiting(tmp_path)
    repo, pr = FakeRepository(), DraftPR(fail_check=True)
    publisher = _publisher(store, run, repo=repo, pr=pr, rebuild=lambda _: package)
    with pytest.raises(PublicationBlocked):
        publisher.publish(run)
    repo.remote_oid = "b" * 40
    pr.fail_check = False
    with pytest.raises(PublicationBlocked, match="no longer matches"):
        publisher.publish(store.load(run.run_id))
    assert pr.pending == [] and pr.created == []


def test_group_draft_retry_revalidates_previously_created_draft(tmp_path):
    store, run, approval, _ = _waiting_group(tmp_path)
    run = run.validated_update(review=CodexResult(summary="Local review passed", verification_needs=(need(),)))
    run = run.validated_update(verification_plan=v.plan(run, ()))
    package = issue_approval(h.prepare(run, approval), approved_by="operator")
    run = store.save(run.validated_update(approval=package), run.version)
    class FailSecondOnce(DraftPR):
        failed = False
        def create(self, *, repo_url, **kwargs):
            if repo_url.endswith("app.git") and not self.failed:
                self.failed = True
                raise RuntimeError("provider unavailable")
            return super().create(repo_url=repo_url, **kwargs)
    pr, repo = FailSecondOnce(), GroupRepository()
    publisher = Publisher(store, repo, lambda _: package, pr, GroupCommenter(), "github", "github.example")
    partial = publisher.publish(run)
    assert partial.state is WorkflowState.PARTIAL_SUCCESS
    assert len(pr.created) == 1
    delivered = publisher.publish(partial)
    assert delivered.state is WorkflowState.WAITING_PR_VERIFICATION
    assert len(pr.created) == 2
    assert [operation for operation, _ in repo.calls].count("commit") == 2


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_provider_creates_native_draft_and_commit_bound_pending_check(provider):
    seen = []
    sha = "a" * 40
    def handler(request):
        payload = json.loads(request.content)
        seen.append((str(request.url), payload))
        if "/statuses/" in str(request.url):
            return httpx.Response(201, json={"state": "pending", "status": "pending",
                "context": h.CHECK_CONTEXT, "name": h.CHECK_CONTEXT})
        return httpx.Response(201, json={"draft": True, "head": {"sha": sha}, "sha": sha,
            "html_url": "https://github.example/x/y/pull/1", "web_url": "https://gitlab.example/x/y/-/merge_requests/1"})
    client = _client(provider, handler)
    repo = f"https://{provider}.example/x/y.git"
    client.set_verification_pending(repo_url=repo, sha=sha, branch="feature")
    client.create(repo_url=repo, head="feature", base="main", title="fix", body="pending", marker="run",
                  draft=True, expected_sha=sha)
    assert sha in seen[0][0]
    assert seen[0][1]["state"] == "pending"
    assert seen[1][1].get("draft") is True if provider == "github" else seen[1][1]["title"].startswith("Draft:")


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("bad", ["draft", "sha"])
def test_recovery_refuses_non_draft_or_different_commit(provider, bad):
    sha = "a" * 40
    item = {"draft": bad != "draft", "head": {"ref": "feature", "sha": "b" * 40 if bad == "sha" else sha},
            "base": {"ref": "main"}, "body": "run", "description": "run", "source_branch": "feature",
            "target_branch": "main", "sha": "b" * 40 if bad == "sha" else sha}
    client = _client(provider, lambda _: httpx.Response(200, json=[item]))
    with pytest.raises(PullRequestProviderError, match="draft"):
        client.find(repo_url=f"https://{provider}.example/x/y.git", head="feature", base="main", marker="run",
                    draft=True, expected_sha=sha)


def test_unbound_draft_request_has_zero_http_effects():
    client = _client("github", lambda _: pytest.fail("must not make HTTP request"))
    with pytest.raises(PullRequestProviderError, match="identity"):
        client.create(repo_url="https://github.example/x/y.git", head="feature", base="main",
                      title="fix", body="pending", marker="run", draft=True)


@pytest.mark.asyncio
async def test_old_task_handoff_button_generates_approval_without_publishing(tmp_path):
    from textual.widgets import Button
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.verification_modal import VerificationModal
    from test_developer_workflow_tui_app import FakeController

    _, _, _, _, run = reviewed(tmp_path)
    detail = replace(RunDetail.from_run(run), can_defer_verification=True)
    calls = []
    class Controller(FakeController):
        def __init__(self):
            super().__init__()
            self.runs = (detail.summary,)
        def show(self, _):
            return detail
        def verification_nodes(self):
            return ()
        def resume(self, run_id, version):
            calls.append((run_id, version))
            return detail
        def approve(self, *args, **kwargs):
            pytest.fail("handoff must not implicitly approve")
        def verify(self, *args, **kwargs):
            pytest.fail("handoff must not fabricate a verification")
    app = DeveloperWorkflowTuiApp(Controller(), 3)
    async with app.run_test(size=(120, 48)) as pilot:
        await pilot.pause()
        await pilot.click("#action-resume")
        await pilot.pause()
        assert isinstance(app.screen, VerificationModal)
        assert app.screen.query_one("#verification-defer", Button).display
        app.screen._defer()
        for _ in range(20):
            await pilot.pause(0.05)
            if calls:
                break
        assert calls == [(run.run_id, run.version)]


@pytest.mark.asyncio
async def test_waiting_draft_approval_has_visible_button(tmp_path):
    from textual.widgets import Button
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from test_developer_workflow_tui_app import FakeController

    _, _, run = deferred(tmp_path)
    detail = RunDetail.from_run(run)
    class Controller(FakeController):
        def __init__(self):
            super().__init__()
            self.runs = (detail.summary,)
        def show(self, _):
            return detail
    app = DeveloperWorkflowTuiApp(Controller(), 3)
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        button = app.screen.query_one("#action-approve", Button)
        assert button.display and "Draft PR" in button.label.plain
        assert app.screen.query_one("#action-bar").display
