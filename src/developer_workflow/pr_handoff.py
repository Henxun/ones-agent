"""Deliver unfinished external checks to a Draft PR without claiming they passed."""
from __future__ import annotations

from . import verification
from .contracts import ApprovalPackage, WorkflowRun, WorkflowType
from .verification_models import VerificationTask

PENDING_STATUSES = {"manual", "ready", "waiting_environment"}
CHECK_CONTEXT = "ones-agent/external-verification"


def blocking_reason(tasks: tuple[VerificationTask, ...], *, defer: bool) -> str:
    if defer and all(task.status == "passed" or task.status in PENDING_STATUSES for task in tasks):
        return ""
    return verification.pending_reason(tasks)


def pending(run: WorkflowRun) -> tuple[VerificationTask, ...]:
    return tuple(task for task in run.verification_plan if task.status != "passed")


def merge_readiness_binding(run: WorkflowRun) -> tuple[str, str]:
    """Return immutable verification/publication digests for a delivered Draft.

    The publication digest covers every changed repository's exact PR and
    commit identity.  It intentionally does not imply that a platform merge or
    a release artifact exists.
    """

    approval = run.approval
    if (
        run.state.value != "WAITING_PR_VERIFICATION"
        or approval is None
        or not approval.draft_pr
        or not approval.fingerprint
        or not approval.deferred_verification
    ):
        raise ValueError("merge readiness requires a delivered Draft PR handoff")
    verification_digest = verification.digest(
        tuple(task.model_dump(mode="json") for task in approval.deferred_verification)
    )
    if run.group_publication is not None:
        publications = run.group_publication.repositories
        repository_facts = tuple(
            {
                "repository_key": item.repository_key,
                "approved_fingerprint": item.approved_fingerprint,
                "commit_hash": item.commit_hash,
                "pr_url": item.pr_url,
            }
            for item in publications
        )
        complete = bool(publications) and all(
            item.approved_fingerprint == approval.fingerprint
            and item.commit_hash
            and item.pr_url
            and not item.error
            for item in publications
        )
    else:
        publication = run.publication
        repository_key = run.repository.key if run.repository is not None else ""
        repository_facts = (
            {
                "repository_key": repository_key,
                "approved_fingerprint": publication.approved_fingerprint,
                "commit_hash": publication.commit_hash,
                "pr_url": publication.pr_url,
            },
        )
        complete = bool(repository_key) and (
            publication.approved_fingerprint == approval.fingerprint
            and bool(publication.commit_hash)
            and bool(publication.pr_url)
            and not publication.error
        )
    if not complete:
        raise ValueError("merge readiness publication evidence is incomplete")
    return verification_digest, verification.digest(repository_facts)


def prepare(run: WorkflowRun, package: ApprovalPackage) -> ApprovalPackage:
    tasks = pending(run)
    if not tasks:
        return package
    if blocking_reason(run.verification_plan, defer=True):
        raise ValueError("Failed verification cannot be deferred to PR review")
    # Quote untrusted review text; never let it create checked checklist items.
    def quote(value: str) -> str:
        return "\n".join("> " + line for line in verification.public_text(value, 4096).splitlines())

    lines = ["## 待人工验证（Draft PR）", "",
             "本地测试与代码审查已通过；以下验证尚未通过。本 PR 不代表缺陷已在实机或发布包中修复。",
             "验证人须对本 PR 的实际提交 SHA 记录环境、步骤、结果和证据；代码变化后必须重新验证。",
             f"仓库管理员须配置必需检查 `{CHECK_CONTEXT}`；本工具不会修改分支保护或自动合并/发布。",
             "复选框仅作清单，不会自动放行检查。发布包验证仍须在实际制品上完成。", ""]
    for task in tasks:
        lines.extend([f"- [ ] 验证项 `{task.key}`", quote(task.need.description),
                      quote("环境：" + (", ".join(task.need.capabilities) or "由验证人确认")),
                      quote("验收标准：" + (task.need.acceptance or "需由验证人补充具体步骤与通过标准")),
                      f"> 证据快照：`{task.snapshot_digest}`", ""])
    checklist = "\n".join(lines)
    return package.validated_update(
        deferred_verification=tasks,
        baseline_evidence_missing=run.type is WorkflowType.DEFECT and not run.pre_fix_test_results,
        manual_checks=tuple(dict.fromkeys((*package.manual_checks, *(task.need.description for task in tasks)))),
        pr_body=package.pr_body + "\n\n" + checklist if package.repository_group is None else package.pr_body,
        repositories=tuple(item.validated_update(pr_body=item.pr_body + "\n\n" + checklist)
                           if item.changed_files else item for item in package.repositories),
    )


def assert_bound(run: WorkflowRun) -> None:
    """Require an exact partition of reviewed requirements into passed and deferred."""
    package = run.approval
    missing = run.type is WorkflowType.DEFECT and not run.verification_only and not run.pre_fix_test_results
    if package is not None and package.baseline_evidence_missing != missing:
        raise ValueError("Baseline evidence disclosure does not match recorded tests")
    needs = verification.requirements(run)
    if not needs:
        if package is not None and package.deferred_verification:
            raise ValueError("Deferred verification lacks review requirements")
        return
    snapshot = verification.snapshot_digest(run)
    expected = {verification.digest(need.model_dump(mode="json"))[:24]: need for need in needs}
    tasks = run.verification_plan
    if (package is None or len(tasks) != len(expected)
            or {task.key: task.need for task in tasks} != expected
            or any(task.snapshot_digest != snapshot for task in tasks)
            or blocking_reason(tasks, defer=True)
            or package.deferred_verification != pending(run)
            or package.verification_records != verification.records_for_approval(run)):
        raise ValueError("External verification evidence or PR handoff is incomplete or stale")
