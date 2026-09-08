from __future__ import annotations

import json
from pathlib import Path
from traceback import TracebackException

import pytest
from pydantic import ValidationError

from src.developer_workflow import config as workflow_config
from src.developer_workflow.config import (
    ConfigValidationError,
    ConfigSecretError,
    DeveloperWorkflowConfig,
    PublishingConfig,
    RepositoryMappingNotFound,
)


def _write_config(path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "run_root": "runs",
        "worktree_root": "worktrees",
        "mirror_root": "mirrors",
        "sandbox_permission_profile": "ones-worktree-tests",
        "max_codex_attempts": 3,
        "repositories": [
            {
                "key": "project-default",
                "project_id": "project",
                "iteration_id": "*",
                "repo_url": "https://example.invalid/default.git",
                "repo_name": "default",
            },
            {
                "key": "project-iteration",
                "project_id": "project",
                "iteration_id": "iteration",
                "repo_url": "git@example.invalid:team/exact.git",
                "repo_name": "exact",
            },
        ],
        "publishing": {
            "provider": "local_fake",
            "default_target_branch": "main",
            "commit_template": "feat: {summary}",
            "pr_title_template": "{summary}",
            "pr_body_template": "{body}",
        },
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _assert_provenance_error_has_no_project_canary(
    error: ValidationError, canary: str
) -> None:
    assert error.errors()[0]["loc"] == ("sandbox_permission_profile_source",)
    assert error.__cause__ is None
    assert error.__context__ is None
    traceback = TracebackException.from_exception(error, capture_locals=True)
    for frame in traceback.stack:
        filename = frame.filename.replace("\\", "/")
        if filename.endswith("src/developer_workflow/config.py"):
            assert canary not in "\n".join((frame.locals or {}).values())


def test_load_resolves_relative_paths_and_prefers_exact_mapping(tmp_path: Path) -> None:
    config = DeveloperWorkflowConfig.load(_write_config(tmp_path / "ones-dev.json"))

    assert config.max_coding_agent_attempts == config.max_codex_attempts == 3
    assert config.run_root == (tmp_path / "runs").resolve()
    assert config.worktree_root == (tmp_path / "worktrees").resolve()
    assert config.mirror_root == (tmp_path / "mirrors").resolve()
    assert config.resolve_repository("project", "iteration").key == "project-iteration"
    assert config.resolve_repository("project", "other").key == "project-default"
    with pytest.raises(RepositoryMappingNotFound):
        config.resolve_repository("unknown", "iteration")


def test_mapping_key_must_match_requested_project_and_iteration(tmp_path: Path) -> None:
    config = DeveloperWorkflowConfig.load(_write_config(tmp_path / "ones-dev.json"))

    assert (
        config.resolve_mapping_key("project-default", "project", "any").key
        == "project-default"
    )
    with pytest.raises(RepositoryMappingNotFound):
        config.resolve_mapping_key("project-iteration", "project", "other")
    with pytest.raises(RepositoryMappingNotFound):
        config.resolve_mapping_key("project-default", "other-project", "any")


def _repository_group_payload() -> dict[str, object]:
    return {
        "key": "desktop-suite",
        "project_id": "project",
        "iteration_id": "iteration",
        "primary_repository": "desktop-app",
        "repositories": [
            {
                "key": "shared-sdk",
                "project_id": "project",
                "iteration_id": "iteration",
                "repo_url": "https://example.invalid/team/shared-sdk.git",
                "repo_name": "shared-sdk",
                "role": "dependency",
                "depends_on": [],
            },
            {
                "key": "desktop-app",
                "project_id": "project",
                "iteration_id": "iteration",
                "repo_url": "https://example.invalid/team/desktop-app.git",
                "repo_name": "desktop-app",
                "role": "primary",
                "depends_on": ["shared-sdk"],
            },
        ],
        "integration_test_commands": ["uv run pytest tests/integration"],
    }


def test_config_loads_repository_groups_and_normalizes_legacy_mappings(
    tmp_path: Path,
) -> None:
    config = DeveloperWorkflowConfig.load(
        _write_config(
            tmp_path / "ones-dev.json",
            repositories=[],
            repository_groups=[_repository_group_payload()],
        )
    )

    assert config.resolve_group_key(
        "desktop-suite", "project", "iteration"
    ).topological_keys() == ("shared-sdk", "desktop-app")
    assert config.resolve_repository_group("project", "iteration").key == "desktop-suite"

    legacy = DeveloperWorkflowConfig.load(_write_config(tmp_path / "legacy.json"))
    normalized = legacy.normalized_groups()
    assert tuple(group.key for group in normalized) == (
        "project-default",
        "project-iteration",
    )
    assert normalized[0].primary_repository == "project-default"
    assert len(normalized[0].repositories) == 1


def test_config_rejects_key_or_selector_conflicts_between_legacy_and_groups(
    tmp_path: Path,
) -> None:
    same_key = _repository_group_payload()
    same_key["key"] = "project-default"
    with pytest.raises(ValidationError, match="keys must be unique"):
        DeveloperWorkflowConfig.load(
            _write_config(
                tmp_path / "same-key.json", repository_groups=[same_key]
            )
        )

    with pytest.raises(ValidationError, match="project and iteration mappings must be unique"):
        DeveloperWorkflowConfig.load(
            _write_config(
                tmp_path / "same-selector.json",
                repository_groups=[_repository_group_payload()],
            )
        )


@pytest.mark.parametrize(
    "repositories",
    [
        [
            {
                "key": "duplicate",
                "project_id": "one",
                "iteration_id": "*",
                "repo_url": "https://example.invalid/one.git",
                "repo_name": "one",
            },
            {
                "key": "duplicate",
                "project_id": "two",
                "iteration_id": "*",
                "repo_url": "https://example.invalid/two.git",
                "repo_name": "two",
            },
        ],
        [
            {
                "key": "one",
                "project_id": "same",
                "iteration_id": "same",
                "repo_url": "https://example.invalid/one.git",
                "repo_name": "one",
            },
            {
                "key": "two",
                "project_id": "same",
                "iteration_id": "same",
                "repo_url": "https://example.invalid/two.git",
                "repo_name": "two",
            },
        ],
    ],
)
def test_duplicate_repository_keys_or_pairs_are_rejected(
    tmp_path: Path, repositories: list[dict[str, str]]
) -> None:
    with pytest.raises(ValidationError):
        DeveloperWorkflowConfig.load(
            _write_config(tmp_path / "ones-dev.json", repositories=repositories)
        )


@pytest.mark.parametrize(
    "injected",
    [
        {"password": ""},
        {"nested": {"ToKeN": "value"}},
        {"nested": {"secret": "value"}},
        {"nested": {"PAT": "value"}},
        {"nested": {"credential": "value"}},
        {"nested": [{"API_KEY": "value"}]},
        {"nested": {"Private-Key": "value"}},
        {"AUTHORIZATION": None},
        {"cookie": "value"},
    ],
)
def test_secret_keys_are_rejected_recursively_case_insensitively(
    tmp_path: Path, injected: dict[str, object]
) -> None:
    with pytest.raises(ConfigSecretError):
        DeveloperWorkflowConfig.load(
            _write_config(tmp_path / "ones-dev.json", extra=injected)
        )


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://user:password@example.invalid/repo.git",
        "ssh://user:password@example.invalid/repo.git",
        "relative/repo",
        "ftp://example.invalid/repo.git",
    ],
)
def test_repository_url_rejects_userinfo_and_unsupported_shapes(
    tmp_path: Path, repo_url: str
) -> None:
    repositories = [
        {
            "key": "repo",
            "project_id": "project",
            "iteration_id": "*",
            "repo_url": repo_url,
            "repo_name": "repo",
        }
    ]
    with pytest.raises(ValidationError):
        DeveloperWorkflowConfig.load(
            _write_config(tmp_path / "ones-dev.json", repositories=repositories)
        )


@pytest.mark.parametrize("max_attempts", [0, 11])
def test_max_codex_attempts_is_bounded(tmp_path: Path, max_attempts: int) -> None:
    with pytest.raises(ValidationError):
        DeveloperWorkflowConfig.load(
            _write_config(
                tmp_path / "ones-dev.json", max_codex_attempts=max_attempts
            )
        )


def test_tui_max_concurrency_defaults_to_three(tmp_path: Path) -> None:
    config = DeveloperWorkflowConfig.load(_write_config(tmp_path / "ones-dev.json"))

    assert config.tui_max_concurrency == 3


@pytest.mark.parametrize("value", [0, 9, True, "3"])
def test_tui_max_concurrency_is_strictly_bounded(
    tmp_path: Path, value: object
) -> None:
    with pytest.raises(ValidationError):
        DeveloperWorkflowConfig.load(
            _write_config(tmp_path / "ones-dev.json", tui_max_concurrency=value)
        )


@pytest.mark.parametrize("repositories", [None, []])
def test_repositories_are_required_and_non_empty(
    tmp_path: Path, repositories: list[object] | None
) -> None:
    path = _write_config(tmp_path / "ones-dev.json")
    if repositories is None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["repositories"]
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        _write_config(path, repositories=repositories)
    with pytest.raises(ValidationError):
        DeveloperWorkflowConfig.load(path)


def test_sandbox_permission_profile_is_required(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "ones-dev.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["sandbox_permission_profile"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="managed Codex permissions profile"):
        DeveloperWorkflowConfig.load(path)


def test_legacy_config_without_profile_source_migrates_to_managed(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "ones-dev.json")

    config = DeveloperWorkflowConfig.load(path)

    assert (
        config.sandbox_permission_profile_source
        is workflow_config.SandboxPermissionProfileSource.MANAGED
    )


@pytest.mark.parametrize(
    ("profile", "source"),
    [
        ("managed-dev", "managed"),
        ("ones-dev-workspace", "builtin_workspace"),
    ],
)
def test_sandbox_permission_profile_source_accepts_exact_bindings(
    tmp_path: Path, profile: str, source: str
) -> None:
    config = DeveloperWorkflowConfig.load(
        _write_config(
            tmp_path / "ones-dev.json",
            sandbox_permission_profile=profile,
            sandbox_permission_profile_source=source,
        )
    )

    assert config.sandbox_permission_profile_source.value == source


@pytest.mark.parametrize(
    ("profile", "source", "forbidden_input"),
    [
        ("managed-dev", "builtin_workspace", "managed-dev"),
        ("ones-dev-workspace", "managed", "ones-dev-workspace"),
        ("managed-dev", "unknown-source", "unknown-source"),
    ],
)
def test_sandbox_permission_profile_source_rejects_confused_bindings_without_echoing_input(
    tmp_path: Path, profile: str, source: str, forbidden_input: str
) -> None:
    with pytest.raises(ValidationError) as captured:
        DeveloperWorkflowConfig.load(
            _write_config(
                tmp_path / "ones-dev.json",
                sandbox_permission_profile=profile,
                sandbox_permission_profile_source=source,
            )
        )

    assert forbidden_input not in str(captured.value)


@pytest.mark.parametrize(
    ("profile", "source", "canaries"),
    [
        ("profile-canary", "builtin_workspace", ("profile-canary",)),
        ("managed-dev", "source-canary", ("source-canary",)),
    ],
)
def test_sandbox_permission_profile_source_rejections_redact_structured_errors(
    tmp_path: Path, profile: str, source: str, canaries: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError) as captured:
        DeveloperWorkflowConfig.load(
            _write_config(
                tmp_path / "ones-dev.json",
                sandbox_permission_profile=profile,
                sandbox_permission_profile_source=source,
            )
        )

    error = captured.value
    public_forms = (str(error), repr(error), repr(error.errors()))
    assert all(canary not in form for canary in canaries for form in public_forms)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_provenance_errors_scrub_project_traceback_state_across_config_entrypoints(
    tmp_path: Path,
) -> None:
    canary = "source-canary"
    path = _write_config(
        tmp_path / "ones-dev.json", sandbox_permission_profile_source=canary
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    operations = (
        lambda: DeveloperWorkflowConfig(**payload),
        lambda: DeveloperWorkflowConfig.model_validate(payload),
        lambda: DeveloperWorkflowConfig.model_validate_json(json.dumps(payload)),
        lambda: DeveloperWorkflowConfig.model_validate_strings(payload),
    )

    for operation in operations:
        with pytest.raises(ValidationError) as captured:
            operation()
        _assert_provenance_error_has_no_project_canary(captured.value, canary)


def test_provenance_assignment_error_scrubs_project_traceback_state(tmp_path: Path) -> None:
    canary = "source-canary"
    config = DeveloperWorkflowConfig.load(_write_config(tmp_path / "ones-dev.json"))

    with pytest.raises(ValidationError) as captured:
        config.sandbox_permission_profile_source = canary

    _assert_provenance_error_has_no_project_canary(captured.value, canary)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("sandbox_permission_profile", "ones-dev-workspace"),
        ("sandbox_permission_profile_source", "builtin_workspace"),
    ],
)
def test_profile_source_assignment_is_atomic(
    tmp_path: Path, field_name: str, value: str
) -> None:
    config = DeveloperWorkflowConfig.load(_write_config(tmp_path / "ones-dev.json"))
    before = config.model_dump(mode="json")

    with pytest.raises(ValidationError):
        setattr(config, field_name, value)

    assert config.model_dump(mode="json") == before


def test_config_root_forbids_extra_fields(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DeveloperWorkflowConfig.load(
            _write_config(tmp_path / "ones-dev.json", unknown_policy=True)
        )


def test_publishing_config_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PublishingConfig(provider="github", unknown_policy=True)


@pytest.mark.parametrize("provider", ["github", "gitlab", "local_fake"])
def test_publishing_config_accepts_supported_providers(provider: str) -> None:
    config = PublishingConfig(provider=provider)

    assert config.provider.value == provider


def test_publishing_config_rejects_unknown_provider() -> None:
    with pytest.raises(ValidationError):
        PublishingConfig(provider="bitbucket")


def test_example_configuration_is_secret_free_and_loadable() -> None:
    example = Path("docs/examples/ones-dev.config.json").resolve()
    config = DeveloperWorkflowConfig.load(example)

    assert config.sandbox_permission_profile == "REPLACE_WITH_MANAGED_WORKTREE_TEST_PROFILE"
    assert config.resolve_repository("project-demo", "iteration-demo").key == "demo-exact"
    assert config.resolve_repository("project-demo", "future").key == "demo-default"


@pytest.mark.parametrize(
    "secret_key",
    [
        "github_token",
        "access-token",
        "client secret",
        "ssh_private_key",
        "apiToken",
        "PrivateKey",
        "apikey",
        "privatekey",
    ],
)
def test_compound_secret_keys_are_rejected(tmp_path: Path, secret_key: str) -> None:
    with pytest.raises(ConfigSecretError):
        DeveloperWorkflowConfig.load(
            _write_config(tmp_path / "ones-dev.json", metadata={secret_key: "value"})
        )


@pytest.mark.parametrize("safe_key", ["secretary", "path", "monkey"])
def test_secret_scan_does_not_reject_unrelated_words(tmp_path: Path, safe_key: str) -> None:
    path = _write_config(tmp_path / "ones-dev.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["publishing"][safe_key] = "value"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        path.write_text(json.dumps(payload), encoding="utf-8")
        DeveloperWorkflowConfig.load(path)


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://example.invalid/repo.git?token=not-inspected",
        "https://example.invalid/repo.git#fragment",
        "https://example.invalid/repo.git\nnext",
        "git@example.invalid:team/repo.git\tbad",
    ],
)
def test_repository_url_rejects_query_fragment_and_controls(
    tmp_path: Path, repo_url: str
) -> None:
    repositories = [
        {
            "key": "repo",
            "project_id": "project",
            "iteration_id": "*",
            "repo_url": repo_url,
            "repo_name": "repo",
        }
    ]
    with pytest.raises(ValidationError):
        DeveloperWorkflowConfig.load(
            _write_config(tmp_path / "ones-dev.json", repositories=repositories)
        )


@pytest.mark.parametrize(
    "template",
    [
        "",
        "   ",
        "{unknown}",
        "{title.upper}",
        "{title[0]}",
        "{title!r}",
        "{title:>20}",
    ],
)
def test_publishing_templates_reject_unsafe_forms(template: str) -> None:
    with pytest.raises(ValidationError):
        PublishingConfig(provider="github", commit_template=template)


@pytest.mark.parametrize(
    "field",
    ["run_id", "work_item_id", "number", "title", "summary", "branch", "base_branch", "pr_url"],
)
def test_publishing_templates_accept_supported_fields(field: str) -> None:
    config = PublishingConfig(provider="gitlab", commit_template="{" + field + "}")

    assert config.commit_template == "{" + field + "}"


def test_pr_body_template_allows_body_but_other_templates_do_not() -> None:
    config = PublishingConfig(provider="github", pr_body_template="{body}")

    assert config.pr_body_template == "{body}"
    assert PublishingConfig(provider="github").pr_body_template == "{body}"
    with pytest.raises(ValidationError):
        PublishingConfig(provider="github", commit_template="{body}")
    with pytest.raises(ValidationError):
        PublishingConfig(provider="github", pr_title_template="{body}")


@pytest.mark.parametrize(
    "branch",
    [
        "",
        " feature",
        "-feature",
        "feature..next",
        "feature@{next",
        "feature~1",
        "feature^next",
        "feature:next",
        "feature?next",
        "feature*next",
        "feature[next",
        "feature\\next",
        "feature//next",
        "feature/.hidden",
        "feature/next.lock",
        "feature/next.",
        "feature/",
        "@",
    ],
)
def test_target_branch_rejects_unsafe_git_refs(branch: str) -> None:
    with pytest.raises(ValidationError):
        PublishingConfig(provider="local_fake", default_target_branch=branch)


@pytest.mark.parametrize("branch", ["main", "feature/REQ-1_safe", "release/2026.08"])
def test_target_branch_accepts_safe_git_refs(branch: str) -> None:
    config = PublishingConfig(provider="github", default_target_branch=branch)

    assert config.default_target_branch == branch


def test_missing_root_path_raises_stable_config_error(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "ones-dev.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["run_root"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((ConfigValidationError, ValidationError)):
        DeveloperWorkflowConfig.load(path)


def test_package_root_exports_public_api() -> None:
    import src.developer_workflow as public

    for name in (
        "ApprovalPackage",
        "CodexResult",
        "CommandResult",
        "ConfigSecretError",
        "ConfigValidationError",
        "DeveloperWorkflowConfig",
        "PublishingConfig",
        "PublishingProvider",
        "RepositoryMapping",
        "RepositoryMappingNotFound",
        "WorkflowRun",
        "WorkflowModel",
        "WorkflowState",
        "WorkflowType",
        "validate_git_ref_name",
    ):
        assert hasattr(public, name)
