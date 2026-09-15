from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from traceback import TracebackException

import pytest
from pydantic import ValidationError

from src.developer_workflow import config as workflow_config
from src.developer_workflow.config import DeveloperWorkflowConfig, PublishingConfig
from src.developer_workflow.contracts import RepositoryMapping
from src.developer_workflow.setup_models import (
    ActiveSetup,
    DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE,
    RuntimeInputs,
    RuntimePublicConfig,
    RuntimeSecrets,
    SecretKind,
    SetupDocument,
    SetupValidationError,
    WorkflowDraft,
)


def test_legacy_default_comment_path_is_migrated_to_messages_endpoint() -> None:
    config = _public_config(
        ones_comment_list_path_template=(
            "/project/api/project/team/{team_id}/task/{item_id}/comments"
        )
    )

    assert (
        config.ones_comment_list_path_template
        == DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE
    )


def _public_config(**overrides: object) -> RuntimePublicConfig:
    payload: dict[str, object] = {
        "ones_base_url": "https://ones.example.invalid",
        "ones_team_id": "team-1",
        "ones_issue_type_id": "issue-type-1",
        "ones_comment_list_path_template": (
            "/project/api/project/team/{team_id}/task/{item_id}/comment"
        ),
        "provider_host": "github.example.invalid",
        "provider_api_url": "https://github.example.invalid/api/v3",
        "git_author_name": "ONES Agent",
        "git_author_email": "agent@example.invalid",
        "codex_auth_mode": "credential",
        "codex_home": None,
    }
    payload.update(overrides)
    return RuntimePublicConfig.model_validate(payload)


def _secret_bundle() -> RuntimeSecrets:
    return RuntimeSecrets(
        {
            SecretKind.ONES_EMAIL: "agent@example.invalid",
            SecretKind.ONES_PASSWORD: "TOKEN-SECRET",
        }
    )


def _repository() -> RepositoryMapping:
    return RepositoryMapping(
        key="repo",
        project_id="project",
        iteration_id="iteration",
        repo_url="https://example.invalid/repo.git",
        repo_name="repo",
    )


def _workflow_config(tmp_path: Path) -> DeveloperWorkflowConfig:
    return DeveloperWorkflowConfig(
        run_root=tmp_path / "runs",
        worktree_root=tmp_path / "worktrees",
        mirror_root=tmp_path / "mirrors",
        sandbox_permission_profile="ones-worktree-tests",
        max_codex_attempts=3,
        repositories=(_repository(),),
        publishing=PublishingConfig(provider="local_fake"),
    )


def _active_payload(**workflow_overrides: object) -> dict[str, object]:
    workflow: dict[str, object] = {
        "run_root": "C:/runs",
        "worktree_root": "C:/worktrees",
        "mirror_root": "C:/mirrors",
        "sandbox_permission_profile": "ones-worktree-tests",
        "max_codex_attempts": 3,
        "tui_max_concurrency": 3,
        "repositories": (_repository().model_dump(mode="json"),),
        "publishing": PublishingConfig(provider="local_fake").model_dump(mode="json"),
    }
    workflow.update(workflow_overrides)
    return {
        "generation": "a" * 32,
        "runtime": _public_config().model_dump(mode="json"),
        "workflow": workflow,
        "credential_kinds": (SecretKind.ONES_PASSWORD,),
    }


def _assert_validation_error_is_sanitized(
    error: ValidationError, secret: str
) -> None:
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in repr(error.errors())
    assert all(item.get("input") == "<redacted>" for item in error.errors())
    assert all(
        item["loc"] == ("<redacted>",)
        or all(secret not in str(part) for part in item["loc"])
        for item in error.errors()
    )


def test_setup_document_never_accepts_secret_fields() -> None:
    payload = {
        "schema_version": 1,
        "profile_id": "default",
        "draft": {"ones_password": "TOKEN-SECRET"},
    }
    with pytest.raises(ValidationError):
        SetupDocument.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "profile_id": "default",
            "draft": {"ones_password": "TOKEN-SECRET"},
        },
        {
            "schema_version": 1,
            "profile_id": "default",
            "draft": {
                "runtime": {
                    **_public_config().model_dump(mode="json"),
                    "provider_api_url": (
                        "https://user:TOKEN-SECRET@github.example.invalid/api"
                    ),
                }
            },
        },
    ],
)
def test_setup_validation_errors_never_echo_rejected_secret_input(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError) as captured:
        SetupDocument.model_validate(payload)

    error = captured.value
    assert "TOKEN-SECRET" not in str(error)
    assert "TOKEN-SECRET" not in repr(error)
    assert "TOKEN-SECRET" not in repr(error.errors())
    assert all(item.get("input") == "<redacted>" for item in error.errors())


def test_runtime_inputs_keep_secrets_out_of_model_dump() -> None:
    inputs = RuntimeInputs(public=_public_config(), secrets=_secret_bundle())
    assert "TOKEN-SECRET" not in repr(inputs)
    assert not hasattr(inputs, "model_dump")


def test_secret_kind_is_a_fixed_allowlist() -> None:
    assert {kind.value for kind in SecretKind} == {
        "ones_email",
        "ones_password",
        "provider_token",
        "codex_api_key",
        "codex_auth_token",
        "git_askpass",
        "git_ssh",
        "git_ssh_command",
        "ssh_askpass",
        "ssh_auth_sock",
    }


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("ones_base_url", 123),
        ("ones_team_id", 123),
        ("provider_host", 123),
        ("git_author_email", 123),
        ("codex_auth_mode", True),
    ],
)
def test_runtime_public_config_rejects_type_coercion(
    field_name: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        _public_config(**{field_name: value})


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("ones_team_id", "team\x00evil"),
        ("ones_issue_type_id", "issue\u200bevil"),
        ("git_author_name", "agent\u2028evil"),
        ("provider_host", "github.example.invalid\u202eevil"),
    ],
)
def test_runtime_public_config_rejects_control_and_format_characters(
    field_name: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        _public_config(**{field_name: value})


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("ones_base_url", "ftp://ones.example.invalid"),
        ("ones_base_url", "https://user:password@ones.example.invalid"),
        ("provider_api_url", "ftp://github.example.invalid/api"),
        ("provider_api_url", "https://github.example.invalid/api?token=value"),
        ("provider_api_url", "https://github.example.invalid:invalid/api"),
        ("provider_host", "https://github.example.invalid"),
        ("ones_comment_list_path_template", "https://ones.example.invalid/comments"),
    ],
)
def test_runtime_public_config_rejects_unsafe_urls_and_hosts(
    field_name: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        _public_config(**{field_name: value})


def test_runtime_public_validation_errors_hide_userinfo() -> None:
    with pytest.raises(ValidationError) as captured:
        RuntimePublicConfig.model_validate(
            {
                **_public_config().model_dump(mode="json"),
                "provider_api_url": (
                    "https://user:TOKEN-SECRET@github.example.invalid/api"
                ),
            }
        )

    assert "TOKEN-SECRET" not in repr(captured.value.errors())


def test_setup_validation_entry_points_all_redact_inputs() -> None:
    payload = {
        **_public_config().model_dump(mode="json"),
        "provider_api_url": "https://user:TOKEN-SECRET@github.example.invalid/api",
    }
    entry_points = (
        lambda: RuntimePublicConfig(**payload),
        lambda: RuntimePublicConfig.model_validate_json(json.dumps(payload)),
    )

    for validate in entry_points:
        with pytest.raises(ValidationError) as captured:
            validate()
        assert "TOKEN-SECRET" not in str(captured.value)
        assert "TOKEN-SECRET" not in repr(captured.value.errors())


def test_frozen_runtime_assignment_uses_safe_validation_details() -> None:
    runtime = _public_config()

    with pytest.raises(ValidationError) as captured:
        runtime.provider_api_url = (  # type: ignore[misc]
            "https://user:TOKEN-SECRET@github.example.invalid/api"
        )

    _assert_validation_error_is_sanitized(captured.value, "TOKEN-SECRET")
    assert captured.value.errors()[0]["loc"] == ("provider_api_url",)


def test_mutable_workflow_assignment_uses_safe_validation_details() -> None:
    draft = WorkflowDraft()
    draft.max_codex_attempts = 4
    assert draft.max_codex_attempts == 4

    with pytest.raises(ValidationError) as captured:
        draft.sandbox_permission_profile = "TOKEN-SECRET\x00"

    _assert_validation_error_is_sanitized(captured.value, "TOKEN-SECRET")
    assert captured.value.errors()[0]["loc"] == ("sandbox_permission_profile",)


def test_workflow_draft_preserves_profile_source_through_deep_copy_and_json_round_trip() -> None:
    draft = WorkflowDraft(
        sandbox_permission_profile="ones-dev-workspace",
        sandbox_permission_profile_source="builtin_workspace",
    )
    copied = draft.model_copy(deep=True)
    payload = copied.model_dump(mode="json")
    restored = WorkflowDraft.model_validate(payload)

    assert (
        copied.sandbox_permission_profile_source
        is workflow_config.SandboxPermissionProfileSource.BUILTIN_WORKSPACE
    )
    assert payload["sandbox_permission_profile_source"] == "builtin_workspace"
    assert (
        restored.sandbox_permission_profile_source
        is workflow_config.SandboxPermissionProfileSource.BUILTIN_WORKSPACE
    )


def _assert_provenance_error_has_no_project_canary(
    error: ValidationError, canary: str
) -> None:
    assert error.errors()[0]["loc"] == ("sandbox_permission_profile_source",)
    assert error.__cause__ is None
    assert error.__context__ is None
    traceback = TracebackException.from_exception(error, capture_locals=True)
    for frame in traceback.stack:
        filename = frame.filename.replace("\\", "/")
        if filename.endswith(
            ("src/developer_workflow/config.py", "src/developer_workflow/setup_models.py")
        ):
            assert canary not in "\n".join((frame.locals or {}).values())

@pytest.mark.parametrize(
    ("profile", "source"),
    [
        ("managed-dev", "builtin_workspace"),
        ("ones-dev-workspace", "managed"),
    ],
)
def test_workflow_draft_rejects_confused_profile_source_bindings(
    profile: str, source: str
) -> None:
    with pytest.raises(ValidationError):
        WorkflowDraft(
            sandbox_permission_profile=profile,
            sandbox_permission_profile_source=source,
        )


def test_workflow_draft_rejects_builtin_source_without_a_builtin_profile() -> None:
    with pytest.raises(ValidationError):
        WorkflowDraft(sandbox_permission_profile_source="builtin_workspace")


@pytest.mark.parametrize(
    ("profile", "source", "canary"),
    [
        ("profile-canary", "builtin_workspace", "profile-canary"),
        ("managed-dev", "source-canary", "source-canary"),
    ],
)
def test_workflow_draft_provenance_errors_detach_the_original_exception(
    profile: str, source: str, canary: str
) -> None:
    with pytest.raises(ValidationError) as captured:
        WorkflowDraft(
            sandbox_permission_profile=profile,
            sandbox_permission_profile_source=source,
        )

    error = captured.value
    _assert_provenance_error_has_no_project_canary(error, canary)


def test_workflow_draft_provenance_assignment_scrubs_project_traceback_state() -> None:
    canary = "source-canary"
    draft = WorkflowDraft(sandbox_permission_profile="managed-dev")

    with pytest.raises(ValidationError) as captured:
        draft.sandbox_permission_profile_source = canary

    _assert_provenance_error_has_no_project_canary(captured.value, canary)


def test_workflow_draft_provenance_errors_scrub_all_validation_entrypoints() -> None:
    canary = "source-canary"
    payload = {
        "sandbox_permission_profile": "managed-dev",
        "sandbox_permission_profile_source": canary,
    }
    operations = (
        lambda: WorkflowDraft(**payload),
        lambda: WorkflowDraft.model_validate(payload),
        lambda: WorkflowDraft.model_validate_json(json.dumps(payload)),
        lambda: WorkflowDraft.model_validate_strings(payload),
    )

    for operation in operations:
        with pytest.raises(ValidationError) as captured:
            operation()
        _assert_provenance_error_has_no_project_canary(captured.value, canary)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("sandbox_permission_profile", "ones-dev-workspace"),
        ("sandbox_permission_profile_source", "builtin_workspace"),
    ],
)
def test_workflow_draft_profile_source_assignment_is_atomic(
    field_name: str, value: str
) -> None:
    draft = WorkflowDraft(sandbox_permission_profile="managed-dev")
    before = draft.model_dump(mode="json")

    with pytest.raises(ValidationError):
        setattr(draft, field_name, value)

    assert draft.model_dump(mode="json") == before


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("sandbox_permission_profile", "ones-dev-workspace"),
        ("sandbox_permission_profile_source", "builtin_workspace"),
    ],
)
def test_committed_workflow_provenance_assignment_remains_frozen(
    tmp_path: Path, field_name: str, value: str
) -> None:
    active = ActiveSetup(
        generation="a" * 32,
        runtime=_public_config(),
        workflow=_workflow_config(tmp_path),
        credential_kinds=(SecretKind.ONES_PASSWORD,),
    )
    workflow = active.workflow
    before = workflow.model_dump(mode="json")

    with pytest.raises(ValidationError) as captured:
        setattr(workflow, field_name, value)

    assert captured.value.errors()[0]["type"] == "frozen_instance"
    assert workflow.model_dump(mode="json") == before


def test_extra_field_name_is_redacted_from_validation_location() -> None:
    with pytest.raises(ValidationError) as captured:
        SetupDocument.model_validate(
            {
                "schema_version": 1,
                "profile_id": "default",
                "TOKEN-SECRET": "value",
            }
        )

    _assert_validation_error_is_sanitized(captured.value, "TOKEN-SECRET")
    assert captured.value.errors()[0]["loc"] == ("<redacted>",)


@pytest.mark.parametrize("field_name", ["ones_base_url", "provider_api_url"])
@pytest.mark.parametrize(
    "value",
    [
        "https://exa mple.invalid/api",
        "https://example.invalid/bad path",
        "https://example.invalid\\api",
        "https://-bad.example.invalid/api",
        "https://bad-.example.invalid/api",
        "https://bad_name.example.invalid/api",
        "https://bad..example.invalid/api",
    ],
)
def test_runtime_urls_reject_ambiguous_hosts_and_paths(
    field_name: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        _public_config(**{field_name: value})


@pytest.mark.parametrize(
    "host",
    [
        "GitHub.example.invalid",
        "-github.example.invalid",
        "github-.example.invalid",
        "git_hub.example.invalid",
        "github..example.invalid",
        "github.example.invalid.",
        "github.example.invalid/path",
        "github.example.invalid\\path",
        "github.example.invalid bad",
    ],
)
def test_provider_host_must_be_a_canonical_hostname(host: str) -> None:
    with pytest.raises(ValidationError):
        _public_config(provider_host=host)


def test_provider_url_host_must_match_the_canonical_provider_host() -> None:
    with pytest.raises(ValidationError):
        _public_config(provider_api_url="https://attacker.example.invalid/api")


def test_ones_http_and_ipv6_urls_remain_supported() -> None:
    internal = _public_config(ones_base_url="http://ones.internal:8088")
    ipv6 = _public_config(
        ones_base_url="http://[2001:db8::1]:8088",
        provider_host="2001:db8::2",
        provider_api_url="https://[2001:db8::2]/api",
    )

    assert internal.ones_base_url == "http://ones.internal:8088"
    assert ipv6.provider_host == "2001:db8::2"


def test_provider_url_supports_idna_via_canonical_ascii_host() -> None:
    config = _public_config(
        provider_host="xn--bcher-kva.example",
        provider_api_url="https://bücher.example/api",
    )

    assert config.provider_host == "xn--bcher-kva.example"


@pytest.mark.parametrize(
    "template",
    [
        "/project/{team_id}/bad path/{item_id}",
        "/project/{team_id}\\task/{item_id}",
        "/project/{team_id}//task/{item_id}",
        "/project/{team_id}/../task/{item_id}",
        "/project/{team_id}/task/{unknown}",
        "/project/{team_id}/comments",
        "/project/{team_id}/task/{item_id}?page=1",
    ],
)
def test_comment_path_template_keeps_the_existing_placeholder_contract(
    template: str,
) -> None:
    with pytest.raises(ValidationError):
        _public_config(ones_comment_list_path_template=template)


@pytest.mark.parametrize(
    "email", ["", "agent", "agent @example.invalid", "a<b>@example.invalid"]
)
def test_runtime_public_config_rejects_invalid_git_email(email: str) -> None:
    with pytest.raises(ValidationError):
        _public_config(git_author_email=email)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("codex_home", Path("relative/codex")),
        ("run_root", Path("relative/runs")),
        ("mirror_root", Path("relative/mirrors")),
        ("worktree_root", Path("relative/worktrees")),
    ],
)
def test_setup_paths_must_be_absolute(field_name: str, value: Path) -> None:
    if field_name == "codex_home":
        with pytest.raises(ValidationError):
            _public_config(codex_home=value)
    else:
        with pytest.raises(ValidationError):
            WorkflowDraft.model_validate({field_name: value})


def test_setup_paths_reject_control_characters(tmp_path: Path) -> None:
    unsafe = Path(f"{tmp_path}\x00nested")

    with pytest.raises(ValidationError):
        WorkflowDraft(run_root=unsafe)


@pytest.mark.parametrize("field_name", ["max_codex_attempts", "tui_max_concurrency"])
@pytest.mark.parametrize("value", [True, "3", 3.0])
def test_workflow_draft_integer_fields_are_strict(
    field_name: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        WorkflowDraft.model_validate({field_name: value})


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_codex_attempts", 0),
        ("max_codex_attempts", 11),
        ("tui_max_concurrency", 0),
        ("tui_max_concurrency", 9),
    ],
)
def test_workflow_draft_integer_fields_are_bounded(
    field_name: str, value: int
) -> None:
    with pytest.raises(ValidationError):
        WorkflowDraft.model_validate({field_name: value})


def test_setup_document_is_strict_and_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SetupDocument.model_validate({"schema_version": "1", "profile_id": "default"})
    with pytest.raises(ValidationError):
        SetupDocument.model_validate(
            {"schema_version": 1, "profile_id": "default", "unknown": True}
        )


def test_active_setup_holds_complete_workflow_and_credential_kinds(
    tmp_path: Path,
) -> None:
    active = ActiveSetup(
        generation="a" * 32,
        runtime=_public_config(),
        workflow=_workflow_config(tmp_path),
        credential_kinds=(SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD),
    )

    document = SetupDocument(profile_id="default", active=active)

    assert document.active is not None
    assert document.active.workflow.repositories[0].key == "repo"
    assert document.model_dump(mode="json")["active"]["credential_kinds"] == [
        "ones_email",
        "ones_password",
    ]


@pytest.mark.parametrize(
    "credential_kinds",
    (
        (),
        (SecretKind.ONES_PASSWORD, SecretKind.ONES_PASSWORD),
        (SecretKind.ONES_PASSWORD, 123),
    ),
)
def test_active_setup_requires_nonempty_unique_strict_credential_kinds(
    tmp_path: Path, credential_kinds: tuple[object, ...]
) -> None:
    with pytest.raises(ValidationError):
        ActiveSetup(
            generation="a" * 32,
            runtime=_public_config(),
            workflow=_workflow_config(tmp_path),
            credential_kinds=credential_kinds,
        )


def test_setup_document_rejects_shared_active_previous_generation(
    tmp_path: Path,
) -> None:
    active = ActiveSetup(
        generation="a" * 32,
        runtime=_public_config(),
        workflow=_workflow_config(tmp_path),
        credential_kinds=(SecretKind.ONES_PASSWORD,),
    )

    with pytest.raises(ValidationError):
        SetupDocument(
            profile_id="default",
            active=active,
            previous=active,
            activation_owner_generation="a" * 32,
        )


@pytest.mark.parametrize(
    "workflow_update",
    [
        {"run_root": "relative/runs"},
        {"mirror_root": "relative/mirrors"},
        {"worktree_root": "relative/worktrees"},
        {"max_codex_attempts": True},
        {"max_codex_attempts": "3"},
        {"tui_max_concurrency": True},
        {"tui_max_concurrency": "3"},
    ],
)
def test_active_setup_revalidates_strict_committed_workflow_constraints(
    workflow_update: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ActiveSetup.model_validate(_active_payload(**workflow_update))


def test_committed_setup_is_deeply_immutable() -> None:
    document = SetupDocument.model_validate(
        {"schema_version": 1, "profile_id": "default", "active": _active_payload()}
    )
    assert document.active is not None

    with pytest.raises(ValidationError):
        document.active = None  # type: ignore[misc]
    with pytest.raises(ValidationError):
        document.active.generation = "b" * 32  # type: ignore[misc]
    with pytest.raises(ValidationError):
        document.active.workflow.run_root = Path("C:/changed")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        document.active.workflow.sandbox_permission_profile = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        document.active.workflow.max_codex_attempts = 4  # type: ignore[misc]
    with pytest.raises(ValidationError):
        document.active.workflow.repositories[0].key = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        document.active.workflow.publishing.provider = "github"  # type: ignore[misc]


def test_setup_document_round_trips_its_json_form(tmp_path: Path) -> None:
    document = SetupDocument(
        profile_id="default",
        active=ActiveSetup(
            generation="a" * 32,
            runtime=_public_config(codex_home=(tmp_path / "codex").resolve()),
            workflow=_workflow_config(tmp_path),
            credential_kinds=(SecretKind.ONES_PASSWORD,),
        ),
    )

    restored = SetupDocument.model_validate(document.model_dump(mode="json"))

    assert restored == document


def test_setup_document_round_trips_pending_activation_owner(tmp_path: Path) -> None:
    active = ActiveSetup(
        generation="b" * 32,
        runtime=_public_config(),
        workflow=_workflow_config(tmp_path),
        credential_kinds=(SecretKind.ONES_PASSWORD,),
    )
    previous = active.validated_update(generation="a" * 32)
    document = SetupDocument(
        profile_id="default",
        active=active,
        previous=previous,
        activation_owner_generation="b" * 32,
    )

    restored = SetupDocument.model_validate(document.model_dump(mode="json"))

    assert restored == document


def test_legacy_document_without_activation_owner_is_stable(tmp_path: Path) -> None:
    payload = SetupDocument(
        profile_id="default",
        active=ActiveSetup(
            generation="b" * 32,
            runtime=_public_config(),
            workflow=_workflow_config(tmp_path),
            credential_kinds=(SecretKind.ONES_PASSWORD,),
        ),
    ).model_dump(mode="json")
    payload.pop("activation_owner_generation")
    payload["previous"] = {**payload["active"], "generation": "a" * 32}

    restored = SetupDocument.model_validate(payload)

    assert restored.active is not None
    assert restored.active.generation == "b" * 32
    assert restored.previous is None
    assert restored.activation_owner_generation is None


def test_pending_activation_owner_must_match_active_generation(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SetupDocument(
            profile_id="default",
            active=ActiveSetup(
                generation="b" * 32,
                runtime=_public_config(),
                workflow=_workflow_config(tmp_path),
                credential_kinds=(SecretKind.ONES_PASSWORD,),
            ),
            activation_owner_generation="c" * 32,
        )


def test_runtime_secrets_are_read_only_frozen_and_do_not_leak() -> None:
    source = {SecretKind.ONES_PASSWORD: "TOKEN-SECRET"}
    secrets = RuntimeSecrets(source)
    source[SecretKind.ONES_PASSWORD] = "changed"

    assert secrets.require(SecretKind.ONES_PASSWORD) == "TOKEN-SECRET"
    assert "TOKEN-SECRET" not in repr(secrets)
    with pytest.raises(TypeError):
        secrets.values[SecretKind.ONES_PASSWORD] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        secrets.values = {}  # type: ignore[misc]


def test_runtime_secrets_require_has_a_fixed_missing_error() -> None:
    with pytest.raises(
        SetupValidationError, match="^runtime credential is unavailable$"
    ):
        RuntimeSecrets({}).require(SecretKind.PROVIDER_TOKEN)


def test_runtime_inputs_are_frozen() -> None:
    inputs = RuntimeInputs(public=_public_config(), secrets=_secret_bundle())

    with pytest.raises(FrozenInstanceError):
        inputs.public = _public_config()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        inputs.public.ones_team_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("public", "secrets"),
    [
        ({}, _secret_bundle()),
        (None, _secret_bundle()),
        ("public", _secret_bundle()),
        (_public_config(), {}),
        (_public_config(), None),
        (_public_config(), "secrets"),
    ],
)
def test_runtime_inputs_reject_mutable_or_untyped_injection(
    public: object, secrets: object
) -> None:
    with pytest.raises(TypeError):
        RuntimeInputs(public=public, secrets=secrets)  # type: ignore[arg-type]


@pytest.mark.parametrize("generation", ["x", "../escape", "profile/generation", "A" * 32])
def test_generation_is_a_canonical_credential_target_segment(generation: str) -> None:
    with pytest.raises(ValidationError):
        ActiveSetup(
            generation=generation,
            runtime=_public_config(),
            workflow=DeveloperWorkflowConfig(
                run_root=Path("C:/runs"),
                worktree_root=Path("C:/worktrees"),
                mirror_root=Path("C:/mirrors"),
                sandbox_permission_profile="ones-worktree-tests",
                max_codex_attempts=3,
                repositories=(_repository(),),
                publishing=PublishingConfig(provider="local_fake"),
            ),
            credential_kinds=(SecretKind.ONES_PASSWORD,),
        )


def test_package_root_exports_setup_contracts() -> None:
    import src.developer_workflow as public

    for name in (
        "ActiveSetup",
        "RuntimeInputs",
        "RuntimePublicConfig",
        "RuntimeSecrets",
        "SecretKind",
        "SetupDocument",
        "SetupDraft",
        "SetupValidationError",
        "WorkflowDraft",
    ):
        assert hasattr(public, name)
