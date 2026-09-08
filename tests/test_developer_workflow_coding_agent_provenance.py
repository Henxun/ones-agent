from __future__ import annotations

from src.developer_workflow.contracts import (
    CodingAgentProvenance,
    CodingAgentResult,
    CodexResult,
    WorkflowRun,
    WorkflowType,
)
from src.developer_workflow.tui.detail_rendering import overview
from src.developer_workflow.tui.models import RunDetail


def test_provider_neutral_result_keeps_legacy_type_and_wire_field_compatible() -> None:
    result = CodingAgentResult(summary="reviewed")
    run = WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1").validated_update(
        codex_results=(result,)
    )

    assert CodexResult is CodingAgentResult
    assert run.coding_agent_results == (result,)
    assert "codex_results" in run.model_dump(mode="json")
    assert "coding_agent_results" not in run.model_dump(mode="json")


def test_new_run_records_selected_coding_agent_without_local_path() -> None:
    provenance = CodingAgentProvenance(key="claude", label="Claude Code")

    run = WorkflowRun.new(
        WorkflowType.REQUIREMENT,
        "REQ-1",
        coding_agent=provenance,
    )

    assert run.coding_agent == provenance
    assert run.model_dump(mode="json")["coding_agent"] == {
        "key": "claude",
        "label": "Claude Code",
        "version": "",
    }


def test_legacy_run_without_coding_agent_remains_loadable() -> None:
    serialized = WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1").model_dump(
        mode="json"
    )
    serialized.pop("coding_agent")

    restored = WorkflowRun.model_validate(serialized)

    assert restored.coding_agent is None
    assert "旧任务未记录" in overview(RunDetail.from_run(restored)).plain


def test_overview_displays_persisted_coding_agent_identity() -> None:
    run = WorkflowRun.new(
        WorkflowType.REQUIREMENT,
        "REQ-1",
        coding_agent=CodingAgentProvenance(key="codex", label="Codex"),
    )

    text = overview(RunDetail.from_run(run)).plain

    assert "Coding Agent" in text
    assert "Codex (codex)" in text
    assert "Agent 版本" in text
    assert "尚未采集" in text
