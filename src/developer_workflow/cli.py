"""Safe, thin command-line adapter for the developer workflow orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Callable, Mapping, Protocol, TextIO
from urllib.parse import urlsplit

from .config import DeveloperWorkflowConfig, SandboxPermissionProfileSource
from .codex_runtime import CodexRuntimePreparer
from .contracts import DefectCandidate, WorkflowRun, WorkflowState
from .orchestrator import DeveloperWorkflowOrchestrator


class OrchestratorFactory(Protocol):
    def __call__(self, config: DeveloperWorkflowConfig) -> DeveloperWorkflowOrchestrator: ...


class DefectListClient(Protocol):
    async def list_candidates(
        self,
        project: str,
        iteration: str,
        assignee: str,
        *,
        status_ids: tuple[str, ...] | None = None,
    ) -> tuple[DefectCandidate, ...]: ...

    async def close(self) -> None: ...


class DefectListFactory(Protocol):
    def __call__(self, limit: int, page_size: int) -> DefectListClient: ...


class TuiRunner(Protocol):
    def __call__(self, first: object, second: object) -> None: ...


class TuiHostFactory(Protocol):
    def __call__(self, template_path: Path) -> tuple[object, object]: ...


class SandboxProfileValidator(Protocol):
    def __call__(self, profile: str, environment: dict[str, str]) -> None: ...


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


class _SafeParser(argparse.ArgumentParser):
    """Argparse variant which never reflects an untrusted argv item in an error."""

    output: TextIO
    error_output: TextIO

    def _print_message(self, message: str | None, file: TextIO | None = None) -> None:
        if message:
            _write(self.output, message)

    def error(self, message: str) -> None:
        _write(self.error_output, "error: invalid command arguments\n")
        raise _ParserExit(2)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message:
            _write(self.error_output if status else self.output, message)
        raise _ParserExit(status)


def _parser(stdout: TextIO, stderr: TextIO) -> _SafeParser:
    parser = _SafeParser(prog="ones-dev", description="ONES developer workflows")
    parser.output, parser.error_output = stdout, stderr
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(name: str) -> argparse.ArgumentParser:
        item = subparsers.add_parser(name)
        if isinstance(item, _SafeParser):
            item.output, item.error_output = stdout, stderr
        item.add_argument("--config", default="ones-dev.config.json")
        return item

    requirement = command("requirement")
    requirement.add_argument("requirement_id")
    requirement.add_argument("--mapping")

    defect = command("defect")
    defect.add_argument("--project", required=True)
    defect.add_argument("--iteration", required=True)
    defect.add_argument("--assignee", required=True)
    defect.add_argument("--select")
    defect.add_argument("--mapping")

    defects = subparsers.add_parser("defects")
    if isinstance(defects, _SafeParser):
        defects.output, defects.error_output = stdout, stderr
    defect_commands = defects.add_subparsers(dest="defects_command", required=True)
    defect_list = defect_commands.add_parser("list")
    if isinstance(defect_list, _SafeParser):
        defect_list.output, defect_list.error_output = stdout, stderr
    defect_list.add_argument("--project", required=True)
    defect_list.add_argument("--iteration", required=True)
    defect_list.add_argument("--assignee", required=True)
    defect_list.add_argument("--status")
    defect_list.add_argument("--format", choices=("table", "json"), default="table")
    defect_list.add_argument("--limit", type=int, default=5000)
    defect_list.add_argument("--page-size", type=int, default=200)

    for name in ("show", "resume"):
        item = command(name)
        item.add_argument("run_id")

    command("verification-nodes")
    probe = command("probe-node")
    probe.add_argument("node_key")
    replan = command("plan-verification")
    replan.add_argument("run_id")
    replan.add_argument("--version", type=int, required=True)
    verify = command("verify")
    verify.add_argument("run_id")
    verify.add_argument("--task", required=True)
    verify.add_argument("--actor", required=True)
    verify.add_argument("--version", type=int, required=True)
    verify.add_argument("--recipe-digest")
    verify.add_argument("--manual-evidence")
    verify.add_argument("--failed", action="store_true")

    revise = command("revise")
    revise.add_argument("run_id")
    revise.add_argument("--feedback", required=True)
    revise.add_argument("--scope")

    approve = command("approve")
    approve.add_argument("run_id")
    approve.add_argument("--actor", required=True)

    cancel = command("cancel")
    cancel.add_argument("run_id")
    cancel.add_argument("--actor", required=True)
    command("tui")
    return parser


def _safe_value(value: object, *, maximum: int = 4096) -> str:
    text = str(value)
    result: list[str] = []
    for character in text[:maximum]:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"} or character == "\x1b":
            result.append("?")
        else:
            result.append(character)
    return "".join(result)


def _write(stream: TextIO, text: str) -> None:
    encoding = getattr(stream, "encoding", None)
    if isinstance(encoding, str):
        try:
            text = text.encode(encoding, errors="replace").decode(encoding)
        except (LookupError, UnicodeError):
            text = text.encode("ascii", errors="replace").decode("ascii")
    stream.write(text)


def _line(stream: TextIO, label: str, value: object) -> None:
    _write(stream, f"{label}: {_safe_value(value)}\n")


def _show_run(run: WorkflowRun, stdout: TextIO) -> None:
    """Print only the explicit, secret-free terminal status contract."""

    _line(stdout, "run_id", run.run_id)
    _line(stdout, "state", run.state.value)
    _line(stdout, "version", run.version)
    if run.approval is not None and run.approval.draft_pr:
        _line(stdout, "publication_mode", "Draft PR; external verification pending; no merge/release authorization")
        _line(stdout, "deferred_checks", len(run.approval.deferred_verification))
    for task in run.verification_plan:
        _line(stdout, "verification", f"{task.key} | {task.status} | {task.node_key}/{task.recipe_key} | {task.need.description} | {task.need.acceptance}")
        _line(stdout, "recipe_digest", task.recipe_digest)
    if run.blocked_reason:
        _line(stdout, "blocked_reason", run.blocked_reason)
    if run.worktree_path:
        _line(stdout, "worktree", run.worktree_path)
    for result in run.test_results:
        _line(stdout, "tests", result.summary)
    risks = run.approval.risks if run.approval is not None else ()
    if run.review is not None:
        risks = (*risks, *run.review.risks)
    for risk in dict.fromkeys(risks):
        _line(stdout, "risks", risk)
    if run.approval is not None and run.approval.fingerprint:
        _line(stdout, "fingerprint", run.approval.fingerprint)
    if run.approval is not None and run.approval.repository_group is not None:
        _line(stdout, "repository group", run.approval.repository_group.key)
        for item in run.approval.repositories:
            role = item.mapping.role.value
            _line(
                stdout, "repository evidence",
                f"{item.repository_key} | {role} | "
                f"base {item.base_commit} | head {item.head_commit}",
            )
            _line(stdout, "diff", f"{item.repository_key} | {item.diff_hash}")
            if item.tree_hash:
                _line(stdout, "approved tree", f"{item.repository_key} | {item.tree_hash}")
            _line(stdout, "diff summary", f"{item.repository_key} | {item.diff_summary}")
            for path in item.changed_files:
                _line(stdout, "changed file", f"{item.repository_key}:{path}")
            for result in item.tests:
                _line(stdout, "repository test", f"{item.repository_key} | {result.summary}")
        for result in run.approval.integration_tests:
            _line(stdout, "integration test", result.summary)
    if run.publication.pr_url:
        _line(stdout, "pr_url", run.publication.pr_url)
    if run.group_publication is not None:
        by_key = {
            item.repository_key: item for item in run.group_publication.repositories
        }
        for key in run.group_publication.order:
            item = by_key.get(key)
            if item is None:
                _line(stdout, "repository", f"{key} | unchanged")
                continue
            status = (
                "pr-created" if item.pr_url else
                "pushed" if item.push_completed_at is not None else
                "committed" if item.commit_hash else "pending"
            )
            _line(stdout, "repository", f"{key} | {status}")
            if item.commit_hash:
                _line(stdout, "commit", f"{key} | {item.commit_hash}")
            if item.push_completed_at is not None:
                _line(
                    stdout, "push",
                    f"{key} | {item.push_completed_at.isoformat()}",
                )
            if item.pr_url:
                _line(stdout, "pr_url", item.pr_url)
            if item.error:
                _line(stdout, "repository error", f"{key} | {item.error}")
        if run.group_publication.error:
            _line(stdout, "group publication error", run.group_publication.error)


def _show_candidates(candidates: Sequence[DefectCandidate], stdout: TextIO) -> None:
    for index, item in enumerate(candidates, start=1):
        fields = (item.key, item.uuid, item.priority, item.status, item.title, item.status_id)
        _write(stdout, f"{index}. {' | '.join(_safe_value(value, maximum=512) for value in fields)}\n")


def _candidate_output(candidate: DefectCandidate) -> dict[str, str]:
    return {
        "uuid": candidate.uuid,
        "key": candidate.key,
        "number": candidate.number,
        "title": candidate.title,
        "priority": candidate.priority,
        "status": candidate.status,
        "status_id": candidate.status_id,
        "updated_at": candidate.updated_at,
    }


def _parse_status_ids(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    parts = value.split(",")
    if (
        not 1 <= len(parts) <= 128
        or len(parts) != len(set(parts))
        or any(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", part) is None for part in parts)
    ):
        raise ValueError("status ids are invalid")
    return tuple(parts)


async def _read_defect_list(
    client: DefectListClient,
    project: str,
    iteration: str,
    assignee: str,
    status_ids: tuple[str, ...] | None,
) -> tuple[DefectCandidate, ...]:
    try:
        if status_ids is None:
            result = await client.list_candidates(project, iteration, assignee)
        else:
            result = await client.list_candidates(
                project, iteration, assignee, status_ids=status_ids
            )
        return tuple(result)
    finally:
        await client.close()


def _execute_defect_list(
    args: argparse.Namespace,
    factory: DefectListFactory,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if not (
        1 <= args.limit <= 5000
        and 1 <= args.page_size <= 200
        and args.page_size <= args.limit
    ):
        _write(stderr, "error: invalid pagination bounds\n")
        return 2
    try:
        status_ids = _parse_status_ids(args.status)
    except ValueError:
        _write(stderr, "error: invalid status ids\n")
        return 2
    client = factory(args.limit, args.page_size)
    candidates = asyncio.run(
        _read_defect_list(
            client, args.project, args.iteration, args.assignee, status_ids
        )
    )
    if args.format == "json":
        payload = [_candidate_output(candidate) for candidate in candidates]
        _write(stdout, json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    elif candidates:
        _show_candidates(candidates, stdout)
    else:
        _write(stdout, "No open defects.\n")
    return 0


def _show_repositories(run: WorkflowRun, stdout: TextIO) -> None:
    for mapping in run.repository_candidates:
        fields = (mapping.key, mapping.repo_url, mapping.base_branch)
        _write(stdout, "repository: " + " | ".join(_safe_value(value) for value in fields) + "\n")
        for configured in (*mapping.lint_commands, *mapping.build_commands, *mapping.test_commands):
            _line(stdout, "command", configured)
    for group in run.repository_group_candidates:
        _line(stdout, "repository group", group.key)
        _line(stdout, "primary repository", group.primary_repository)
        _line(
            stdout, "local source policy",
            "source_path is read-only input; changes use isolated managed worktrees",
        )
        by_key = {item.key: item for item in group.repositories}
        for index, key in enumerate(group.topological_keys(), start=1):
            mapping = by_key[key]
            source = (
                str(mapping.source_path)
                if mapping.source_path is not None
                else "remote mirror"
            )
            _write(
                stdout,
                f"  {index}. {_safe_value(key)} | {_safe_value(mapping.role.value)} | "
                f"{_safe_value(mapping.base_branch)} | {_safe_value(source)} | "
                f"{_safe_value(mapping.repo_url)}\n",
            )


def _tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _read_line(stdin: TextIO) -> str | None:
    try:
        value = stdin.readline()
    except (EOFError, OSError):
        return None
    return None if value == "" else value.rstrip("\r\n")


def _confirm_mapping(
    orchestrator: DeveloperWorkflowOrchestrator,
    run: WorkflowRun,
    mapping_key: str | None,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> tuple[int, WorkflowRun]:
    if run.state is not WorkflowState.VALIDATING:
        return 0, run
    _show_repositories(run, stdout)
    selected = mapping_key
    if selected is None:
        if not _tty(stdin):
            _write(stderr, "error: --mapping is required when stdin is not a TTY\n")
            return 2, run
        _write(stdout, "mapping key (blank to reject): ")
        selected = _read_line(stdin)
        if selected is None or selected.casefold() in {"", "n", "no"}:
            return 0, run
        if selected.casefold() in {"y", "yes"}:
            candidates = (*run.repository_candidates, *run.repository_group_candidates)
            if len(candidates) != 1:
                _write(stderr, "error: mapping key is required for multiple candidates\n")
                return 2, run
            selected = candidates[0].key
    result = orchestrator.confirm_repository(run.run_id, selected)
    return 0, result


async def _list_candidates(
    orchestrator: DeveloperWorkflowOrchestrator,
    project: str,
    iteration: str,
    assignee: str,
) -> tuple[DefectCandidate, ...]:
    result = await orchestrator.defect_candidates.list_candidates(
        project, iteration, assignee
    )
    return tuple(result)


def _execute(
    args: argparse.Namespace,
    orchestrator: DeveloperWorkflowOrchestrator,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.command == "requirement":
        run = orchestrator.start_requirement(args.requirement_id)
        code, run = _confirm_mapping(
            orchestrator, run, args.mapping, stdin=stdin, stdout=stdout, stderr=stderr
        )
        if code == 0:
            _show_run(run, stdout)
        return code

    if args.command == "defect":
        candidates = asyncio.run(
            _list_candidates(orchestrator, args.project, args.iteration, args.assignee)
        )
        _show_candidates(candidates, stdout)
        if not candidates:
            _write(stderr, "error: no verified defect candidates\n")
            return 2
        interactive = _tty(stdin)
        selected_id = None if interactive else args.select
        if interactive:
            _write(stdout, "candidate number: ")
            raw = _read_line(stdin)
            if raw is None or not raw.isascii() or not raw.isdigit():
                _write(stderr, "error: candidate selection is invalid\n")
                return 2
            index = int(raw)
            if index < 1 or index > len(candidates):
                _write(stderr, "error: candidate selection is invalid\n")
                return 2
            selected_id = candidates[index - 1].uuid
        elif selected_id is None:
            _write(stderr, "error: --select is required when stdin is not a TTY\n")
            return 2
        matches = [item for item in candidates if item.uuid == selected_id]
        if len(matches) != 1:
            _write(stderr, "error: selected defect is not in the current snapshot\n")
            return 2
        selected = matches[0]
        run = orchestrator.start_defect(
            args.project,
            args.iteration,
            args.assignee,
            selected.snapshot_token,
            selected.uuid,
        )
        code, run = _confirm_mapping(
            orchestrator, run, args.mapping, stdin=stdin, stdout=stdout, stderr=stderr
        )
        if code == 0:
            _show_run(run, stdout)
        return code

    if args.command == "verification-nodes":
        for node in orchestrator.verification_nodes():
            _line(stdout, "node", json.dumps(node, ensure_ascii=False))
        return 0
    if args.command == "probe-node":
        _line(stdout, "node", json.dumps(orchestrator.probe_verification_node(args.node_key)))
        return 0
    if args.command == "plan-verification":
        run = orchestrator.replan_verification(args.run_id, expected_version=args.version)
    elif args.command == "verify":
        if args.failed and not args.manual_evidence:
            raise ValueError("--failed requires --manual-evidence")
        run = orchestrator.verify(args.run_id, args.task, args.actor, expected_version=args.version,
            manual_evidence=args.manual_evidence, passed=not args.failed, expected_recipe_digest=args.recipe_digest)
    elif args.command == "show":
        run = orchestrator.show(args.run_id)
    elif args.command == "resume":
        run = orchestrator.resume(args.run_id)
    elif args.command == "revise":
        run = orchestrator.revise(args.run_id, args.feedback, scope=args.scope)
    elif args.command == "approve":
        run = orchestrator.approve(args.run_id, args.actor)
    elif args.command == "cancel":
        run = orchestrator.cancel(args.run_id, args.actor)
    else:  # guarded by argparse
        raise RuntimeError("command dispatch is unavailable")
    _show_run(run, stdout)
    return 0


def _valid_comment_path_template(value: str | None) -> bool:
    if type(value) is not str or not value.startswith("/"):
        return False
    if (
        "?" in value
        or "#" in value
        or "\\" in value
        or "//" in value
        or any(part in {"", ".", ".."} for part in value[1:].split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    return set(re.findall(r"\{([^{}]+)\}", value)) == {"team_id", "item_id"}


def _valid_runtime_text(value: str) -> bool:
    return bool(value) and value == value.strip() and not any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )


def _validate_runtime_sandbox_permission_profile(
    profile: str,
    source: SandboxPermissionProfileSource,
    environment: Mapping[str, str],
    *,
    codex_preparer: CodexRuntimePreparer | None = None,
) -> None:
    """Prove one explicitly sourced runtime profile without inferring provenance."""

    from .requirement_flow import SandboxCommandExecutor, sandbox_preflight_command

    if type(profile) is not str or type(source) is not SandboxPermissionProfileSource:
        raise RuntimeError("sandbox profile is unavailable")
    try:
        DeveloperWorkflowConfig.validate_sandbox_permission_profile_binding(
            profile,
            source,
        )
    except ValueError:
        raise RuntimeError("sandbox profile is unavailable") from None
    with tempfile.TemporaryDirectory(prefix="ones-dev-sandbox-preflight-") as raw:
        cwd = Path(raw).resolve(strict=True)
        executor_arguments: dict[str, object] = {
            "permission_profile": profile,
            "permission_profile_source": source,
        }
        if codex_preparer is not None:
            executor_arguments["codex_preparer"] = codex_preparer
        executor = SandboxCommandExecutor(**executor_arguments)
        completed = executor(
            sandbox_preflight_command(),
            cwd=cwd,
            env=dict(environment),
            timeout=20,
            max_output_bytes=64 * 1024,
        )
        if completed.returncode != 0:
            raise RuntimeError("sandbox profile is unavailable")


def _validate_sandbox_permission_profile(
    profile: str,
    environment: dict[str, str],
) -> None:
    """Legacy JSON/CLI entrypoint: managed profiles only, with explicit source."""

    _validate_runtime_sandbox_permission_profile(
        profile,
        SandboxPermissionProfileSource.MANAGED,
        environment,
    )


def _legacy_runtime_validator(
    validator: SandboxProfileValidator,
) -> Callable[[str, SandboxPermissionProfileSource, Mapping[str, str]], None]:
    def validate(
        profile: str,
        source: SandboxPermissionProfileSource,
        environment: Mapping[str, str],
    ) -> None:
        if (
            type(profile) is not str
            or type(source) is not SandboxPermissionProfileSource
            or source is not SandboxPermissionProfileSource.MANAGED
        ):
            raise RuntimeError("sandbox profile is unavailable")
        try:
            DeveloperWorkflowConfig.validate_sandbox_permission_profile_binding(
                profile,
                source,
            )
        except ValueError:
            raise RuntimeError("sandbox profile is unavailable") from None
        validator(profile, dict(environment))

    return validate


def build_production_orchestrator(
    config: DeveloperWorkflowConfig,
    *,
    sandbox_profile_validator: SandboxProfileValidator = (
        _validate_sandbox_permission_profile
    ),
) -> DeveloperWorkflowOrchestrator:
    """Build the real local service graph from config plus explicit secret env vars."""
    from config.settings import OnesSettings

    from .runtime_bootstrap import RuntimeBootstrapper, legacy_runtime_inputs

    environment = dict(os.environ)
    active, secrets = legacy_runtime_inputs(
        config, environment, ones_settings=OnesSettings()
    )
    bootstrapper = RuntimeBootstrapper(
        ambient_environment=lambda: environment,
        sandbox_profile_validator=_legacy_runtime_validator(
            sandbox_profile_validator
        ),
    )
    return bootstrapper.build(active, secrets).orchestrator


class _ProductionDefectListClient:
    def __init__(self, gateway: object, candidates: object) -> None:
        self._gateway = gateway
        self._candidates = candidates

    async def list_candidates(
        self,
        project: str,
        iteration: str,
        assignee: str,
        *,
        status_ids: tuple[str, ...] | None = None,
    ) -> tuple[DefectCandidate, ...]:
        result = await self._candidates.list_candidates(
            project, iteration, assignee, status_ids=status_ids
        )
        return tuple(result)

    async def close(self) -> None:
        await self._gateway.close()


def build_production_defect_list_client(
    limit: int, page_size: int
) -> DefectListClient:
    """Build the minimal ONES-only graph used by the read-only list command."""

    from config.settings import OnesSettings
    from src.services.ones_gateway import OnesGateway

    from .defect_flow import DefectCandidateService

    settings = OnesSettings()
    try:
        ones_url = urlsplit(settings.base_url.rstrip("/"))
    except ValueError:
        ones_url = urlsplit("")
    if not (
        ones_url.scheme in {"http", "https"}
        and ones_url.hostname is not None
        and ones_url.username is None
        and ones_url.password is None
        and not ones_url.query
        and not ones_url.fragment
        and _valid_runtime_text(settings.team_id)
        and _valid_runtime_text(settings.issue_type_id)
        and _valid_runtime_text(settings.email)
        and _valid_runtime_text(settings.password)
    ):
        raise RuntimeError("ONES read-only runtime configuration is incomplete")
    gateway = OnesGateway(settings=settings)
    candidates = DefectCandidateService(
        gateway,
        settings.issue_type_id,
        candidate_limit=limit,
        page_size=page_size,
    )
    return _ProductionDefectListClient(gateway, candidates)


def build_production_tui_host(template_path: Path) -> tuple[object, object]:
    """Build only the private setup host; no workflow runtime is opened here."""

    from .codex_runner import CodexRunner, resolve_codex_command
    from .config import (
        BUILTIN_WORKSPACE_PROFILE,
        PublishingConfig,
        PublishingProvider,
    )
    from .contracts import RepositoryMapping
    from .credential_store import create_credential_store
    from .platform_support import default_host_paths
    from .runtime_bootstrap import (
        RuntimeAdapterBundle,
        RuntimeBootstrapper as WorkflowRuntimeBootstrapper,
    )
    from .requirement_flow import DirectConfiguredTestRunner
    from .setup_controller import SetupController
    from .setup_import import (
        ImportDetection,
        SetupImportSourceUnavailable,
        detect_import_sources,
        load_template_workflow,
    )
    from .setup_models import RuntimePublicConfig, SetupDraft, WorkflowDraft
    from .setup_store import SetupStore
    from .tui.setup_screens import SetupImportContext
    from .setup_validation import (
        RuntimeBootstrapper as ValidationBootstrapper,
        SetupStep,
    )

    template_path = Path(template_path)
    environment = dict(os.environ)
    dotenv_path: Path | None = Path.cwd() / ".env"
    try:
        detected = detect_import_sources(
            template_config_path=None,
            dotenv_path=dotenv_path,
            environment=environment,
        )
    except SetupImportSourceUnavailable:
        dotenv_path = None
        detected = detect_import_sources(
            template_config_path=None,
            dotenv_path=dotenv_path,
            environment=environment,
        )
    template = load_template_workflow(template_path)
    detection = ImportDetection(
        environment=detected.environment,
        dotenv=detected.dotenv,
        template_available=template is not None,
    )
    import_context = SetupImportContext(
        detection=detection,
        dotenv_path=dotenv_path,
        template_workflow=(
            None
            if template is None
            else WorkflowDraft.model_validate(
                template.model_dump(mode="python", round_trip=True)
            )
        ),
    )
    # Resolve every host-specific dependency through one fail-closed boundary.
    # This keeps Windows behavior unchanged while ensuring macOS never falls
    # back to a Windows credential backend or an implicit LOCALAPPDATA path.
    host_paths = default_host_paths()
    credentials = create_credential_store(paths=host_paths)
    validation = ValidationBootstrapper.production(
        codex_cache_root=host_paths.cache_root,
    )
    preparer = validation.codex_runtime_preparer
    class _MvpPullRequestClient:
        """Keep MVP analysis/repair local; publishing remains explicitly unavailable."""

        def close(self) -> None:
            return None

        def find(self, **_kwargs: object) -> None:
            raise RuntimeError("publishing is unavailable in MVP mode")

        def create(self, **_kwargs: object) -> str:
            raise RuntimeError("publishing is unavailable in MVP mode")

    def validate_mvp_sandbox_profile(
        profile: str,
        source: SandboxPermissionProfileSource,
        sandbox_environment: Mapping[str, str],
    ) -> None:
        # The MVP does not accept configured shell commands, so its test runner is
        # dormant.  A SandboxCommandExecutor still probes fail-closed before any
        # future command; repeating that probe while merely opening Dashboard
        # incorrectly blocks Windows hosts whose local restricted-token sandbox is
        # unavailable.  Managed profiles retain the full startup validation path.
        if (
            type(profile) is str
            and profile == BUILTIN_WORKSPACE_PROFILE
            and type(source) is SandboxPermissionProfileSource
            and source is SandboxPermissionProfileSource.BUILTIN_WORKSPACE
        ):
            return
        _validate_runtime_sandbox_permission_profile(
            profile,
            source,
            sandbox_environment,
            codex_preparer=preparer,
        )

    def build_unsandboxed_mvp_codex(
        run_root: Path,
        repository: object,
        environment_provider: Callable[[], Mapping[str, str]],
    ) -> CodexRunner:
        return CodexRunner(
            run_root,
            repository,  # type: ignore[arg-type]
            command_resolver=lambda: resolve_codex_command(
                _prepare=preparer.prepare_verified
            ),
            environment_provider=environment_provider,
            sandbox_mode_override="danger-full-access",
        )

    def build_direct_mvp_test_runner(
        _profile: str,
        _source: SandboxPermissionProfileSource,
    ) -> DirectConfiguredTestRunner:
        return DirectConfiguredTestRunner()

    runtime_builder = WorkflowRuntimeBootstrapper(
        codex_runtime_preparer=preparer,
        sandbox_profile_validator=validate_mvp_sandbox_profile,
    )
    store = SetupStore(credentials, config_path=host_paths.config_path)
    runtime_builder.workflow_saver = lambda active, workflow: (
        store.replace_active_workflow("default", active.generation, workflow)
    )

    cwd = Path.cwd().resolve(strict=True)
    workspace = next(
        (candidate for candidate in (cwd, *cwd.parents) if (candidate / ".git").exists()),
        cwd,
    )
    # Repository mirrors and isolated worktrees can be large. Keep the direct
    # MVP runtime beside the checked-out workspace so it uses the same drive,
    # instead of silently exhausting the usually smaller system drive.
    runtime_root = workspace.parent / ".ones-dev-runtime"
    repository_name = re.sub(r"[^A-Za-z0-9._-]+", "-", workspace.name).strip("-.")
    if not repository_name:
        repository_name = "workspace"
    head_path = workspace / ".git" / "HEAD"
    branch = "main"
    try:
        head = head_path.read_text(encoding="ascii").strip()
        if head.startswith("ref: refs/heads/"):
            branch = head.removeprefix("ref: refs/heads/")
    except (OSError, UnicodeError):
        pass
    workflow = WorkflowDraft(
        run_root=runtime_root / "runs",
        mirror_root=runtime_root / "mirrors",
        worktree_root=runtime_root / "worktrees",
        sandbox_permission_profile=BUILTIN_WORKSPACE_PROFILE,
        sandbox_permission_profile_source=(
            SandboxPermissionProfileSource.BUILTIN_WORKSPACE
        ),
        repositories=(
            RepositoryMapping(
                key="workspace",
                project_id="pending-project",
                iteration_id="*",
                repo_url=str(workspace),
                repo_name=repository_name,
                base_branch=branch,
            ),
        ),
        publishing=PublishingConfig(
            provider=PublishingProvider.GITHUB,
            default_target_branch=branch,
        ),
    )
    runtime_builder.adapters = RuntimeAdapterBundle(
        codex_factory=build_unsandboxed_mvp_codex,
        sandbox_factory=build_direct_mvp_test_runner,
        pr_factory=lambda **_kwargs: _MvpPullRequestClient(),
    )
    draft = SetupDraft(
        runtime=RuntimePublicConfig(
            ones_base_url="http://localhost",
            ones_team_id="pending-team",
            ones_issue_type_id="pending-type",
            ones_comment_list_path_template=(
                "/project/api/project/team/{team_id}/task/{item_id}/comments"
            ),
            provider_host="github.com",
            provider_api_url="https://github.com/api/v3",
            git_author_name="ONES Dev Agent",
            git_author_email="ones-dev@localhost",
            codex_auth_mode="file",
            codex_home=None,
        ),
        workflow=workflow,
    )

    def new_setup_controller() -> SetupController:
        return SetupController(
            profile_id="default",
            store=store,
            runtime_builder=runtime_builder,
            runtime_bootstrap=validation,
            draft=draft,
            steps=(SetupStep.ONES, SetupStep.REVIEW),
            activation_timeout=180.0,
        )

    new_setup_controller.import_context = import_context  # type: ignore[attr-defined]

    return new_setup_controller, runtime_builder


def _execute_tui_bootstrap(
    template_path: Path,
    host_factory: TuiHostFactory,
    tui_runner: TuiRunner,
) -> int:
    setup_controller, runtime_bootstrapper = host_factory(template_path)
    try:
        tui_runner(setup_controller, runtime_bootstrapper)
    except BaseException:
        try:
            setup_controller.close()
        except BaseException:
            pass
        raise
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    factory: OrchestratorFactory = build_production_orchestrator,
    defect_list_factory: DefectListFactory = build_production_defect_list_client,
    tui_runner: TuiRunner | None = None,
    tui_host_factory: TuiHostFactory | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = _parser(stdout, stderr)
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except _ParserExit as error:
        return error.status
    try:
        if args.command == "defects":
            return _execute_defect_list(
                args, defect_list_factory, stdout=stdout, stderr=stderr
            )
        if args.command == "tui":
            if tui_runner is None:
                from .tui import run_tui

                tui_runner = run_tui
            return _execute_tui_bootstrap(
                Path(args.config),
                tui_host_factory or build_production_tui_host,
                tui_runner,
            )
        config = DeveloperWorkflowConfig.load(args.config)
        orchestrator = factory(config)
        return _execute(
            args, orchestrator, stdin=stdin, stdout=stdout, stderr=stderr
        )
    except (KeyboardInterrupt, SystemExit):
        _write(stderr, "error: command interrupted safely\n")
        return 130
    except Exception:
        _write(stderr, "error: command failed safely\n")
        return 1


__all__ = [
    "build_production_defect_list_client",
    "build_production_orchestrator",
    "build_production_tui_host",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
