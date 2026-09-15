# PROJECT KNOWLEDGE BASE

## OVERVIEW

ONES agent repository with a Python 3.11 backend, centralized pytest coverage, and spec/process artifacts under `openspec/` and `docs/`.

## STRUCTURE

```text
ones-agent/
├── src/              # backend application package
├── config/           # environment-backed settings outside src/
├── tests/            # centralized backend pytest suite
├── docs/             # architecture and acceptance-rule guidance
├── openspec/         # spec-driven workflow artifacts
├── main.py           # FastAPI app + webhook/API surface
├── server.py         # FastMCP tool server + CLI entry
├── tools.json        # function-calling schema mirroring MCP tools
└── skill.md          # human-readable tool/workflow contract
```

## WHERE TO LOOK

| Task                         | Location                                                                                                              | Notes                                                  |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Backend API route or webhook | `main.py`                                                                                                             | Large integration surface; avoid casual edits          |
| Runtime configuration        | `config/settings.py`                                                                                                  | Top-level settings boundary with env-prefixed families |
| MCP tool behavior            | `server.py`, `tools.json`, `skill.md`                                                                                 | Keep these three aligned                               |
| Runtime workflow/state       | `src/core/`                                                                                                           | Engine, queue, scheduler, store                        |
| ONES + external integrations | `src/integrations/`, `src/services/ones_gateway.py`                                                                   | Gateway/service split matters                          |
| LLM planning or analysis     | `src/llm/`, `src/services/defect_analysis_workflow.py`                                                                | Planner/analyzer boundaries are intentional            |
| Shared backend helpers       | `src/utils/`                                                                                                          | Auth, logging, metrics, audit, retry, secrets          |
| Test expectations            | `tests/`                                                                                                              | Pytest only; async + heavy mocking                     |
| Architecture constraints     | `docs/ones_defect_refactor_boundaries.md`, `docs/analysis_acceptance_rules.md`, `docs/m7_legacy_runtime_retention.md` | Source of anti-pattern rules                           |

## CODE MAP

| Symbol / Surface                | Location                                   | Role                                         |
| ------------------------------- | ------------------------------------------ | -------------------------------------------- |
| `app`                           | `main.py`                                  | FastAPI application entry                    |
| `mcp`                           | `server.py`                                | FastMCP server entry                         |
| `Engine`                        | `src/core/engine.py`                       | Stateful workflow engine + persistence       |
| `Scheduler`                     | `src/core/scheduler.py`                    | Polling and scheduled analysis trigger       |
| `ScheduleManager`               | `src/core/schedule_manager.py`             | Scheduled task CRUD / execution support      |
| `DefectAnalysisWorkflowService` | `src/services/defect_analysis_workflow.py` | Backend analysis orchestration               |
| `OnesGateway`                   | `src/services/ones_gateway.py`             | Canonical ONES access boundary               |
| `ExecutionService`              | `src/services/execution_service.py`        | Execution-phase validation and mutation gate |
| `AnalysisResult` / contracts    | `src/contracts.py`                         | Cross-layer neutral schema surface           |

## GUIDE PLACEMENT RATIONALE

| Path                      | Score  | Why it warranted a guide                                                    |
| ------------------------- | ------ | --------------------------------------------------------------------------- |
| `.`                       | always | Root orientation layer for the backend and terminal application            |
| `src/`                    | 17     | Distinct backend domain with multiple architectural layers                  |
| `src/core/`               | 18     | Stateful runtime hotspot: engine, scheduling, persistence                   |
| `src/services/`           | 20     | Highest backend orchestration density after `main.py`                       |
| `src/utils/`              | 14     | Shared cross-cutting helper surface reused across backend                   |
| `src/integrations/`       | 13     | External adapter boundary with multiple ONES/Git/notification integrations  |
| `src/llm/`                | 12     | Planner/analyzer/prompt boundary with project-specific constraints          |
| `tests/`                  | 16     | Centralized regression boundary with explicit backend safety contracts      |

## CONVENTIONS

- Read `CONTRIBUTING.md` and follow `docs/code_standards.md`, `docs/architecture_standards.md`, and `docs/git_workflow.md` for new or modified code. These general standards do not relax the specialized developer workflow constraints.

- Backend runtime uses Python 3.11+, `from __future__ import annotations`, and explicit typing throughout core files.
- Settings live in `config/settings.py` with separate `BaseSettings` classes and env prefixes (`ONES_`, `GIT_`, `LLM_`, `EMAIL_`, `WECHAT_`, `AGENT_`).
- Treat `config/` as a first-class runtime boundary; environment-backed settings are intentionally kept outside `src/`.
- Root entrypoints are intentionally outside `src/`: `main.py` for FastAPI, `server.py` for MCP.
- Backend tests are centralized in `tests/`; `pyproject.toml` sets `testpaths = ["tests"]` and `asyncio_mode = "auto"`.
- `server.py`, `tools.json`, and `skill.md` form one contract. Tool names, parameters, and semantics must move together.
- Use this file for durable structure/rules only; do not treat it as a per-commit snapshot log.

## ANTI-PATTERNS (THIS PROJECT)

- Before modifying developer workflow gates, review/repair transitions, approvals, publication, or their TUI, read `docs/developer_workflow_constraints.md`. It records user-approved durable constraints; preserve its distinction between Draft PR handoff and merge/release readiness. Add regression coverage for new gates and legacy-task recovery; do not silently reintroduce pre-PR manual-verification requirements.

- Do not merge Planner and Analyzer responsibilities or collapse LLM reasoning into Git mutation logic; see `docs/ones_defect_refactor_boundaries.md`.
- Do not treat polished prose, defect text alone, or repo-resolution confidence as sufficient evidence for high-confidence analysis; see `docs/analysis_acceptance_rules.md`.
- Do not treat `/api/v1/ai/trigger` as the primary workflow. It is a deprecated compatibility shim; use the canonical defect/task routes instead.
- Do not edit only one of `server.py`, `tools.json`, or `skill.md` when changing MCP tools.
- Do not add guidance files in vendored or artifact trees such as `.venv/`, `__pycache__/`, or `data/repos/`.

## UNIQUE STYLES

- Chinese user-facing/backend docstrings are normal in Python surfaces; keep terminology consistent with existing files.
- Backend flow is layered but not fully split by package depth: `main.py` remains a large orchestration shell, while `src/services/` carries newer boundaries.

## COMMANDS

```bash
pytest
python main.py
uv run python server.py
```

## NOTES

- There is no checked-in root CI workflow or Makefile; automation is mostly app-script driven plus `openspec/` / `.opencode/` workflow artifacts.
- `README.md` describes the supported entrypoints; treat code, tests, and docs as the detailed source of truth.
- The React/Vite web frontend has been removed. `main.py` serves APIs and Webhooks only; operator interaction uses the CLI/TUI.

## Spec

- 通过openspec生成规范文档时，使用中文
