"""Strict, secret-free bootstrap configuration and explicit runtime inputs."""

from __future__ import annotations
from .provider_endpoints import provider_api_host_matches

from dataclasses import dataclass, field
from enum import Enum
import ipaddress
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping
import unicodedata
from urllib.parse import urlsplit

from pydantic import (
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .config import (
    DeveloperWorkflowConfig,
    PublishingConfig,
    SandboxPermissionProfileSource,
    _profile_source_validation_error,
)
from .contracts import RepositoryGroupMapping, RepositoryMapping, WorkflowModel
from .verification_models import VerificationNode


class SetupValidationError(ValueError):
    """Raised when bootstrap configuration cannot safely be activated."""


DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE = (
    "/project/api/project/team/{team_id}/task/{item_id}/comments"
)


class SecretKind(str, Enum):
    ONES_EMAIL = "ones_email"
    ONES_PASSWORD = "ones_password"
    PROVIDER_TOKEN = "provider_token"
    CODEX_API_KEY = "codex_api_key"
    CODEX_AUTH_TOKEN = "codex_auth_token"
    GIT_ASKPASS = "git_askpass"
    GIT_SSH = "git_ssh"
    GIT_SSH_COMMAND = "git_ssh_command"
    SSH_ASKPASS = "ssh_askpass"
    SSH_AUTH_SOCK = "ssh_auth_sock"


_BIDI_CONTROLS = {
    "LRE",
    "RLE",
    "LRO",
    "RLO",
    "PDF",
    "LRI",
    "RLI",
    "FSI",
    "PDI",
}
_BIDI_CONTROL_CHARACTERS = {"\u061c", "\u200e", "\u200f"}


def _sanitized_validation_error(
    model: type[WorkflowModel], error: ValidationError
) -> ValidationError:
    known_fields = set(model.model_fields)
    safe_errors = [
        {
            "type": "value_error",
            "loc": (
                ("sandbox_permission_profile_source",)
                if str(item.get("ctx", {}).get("error"))
                == "sandbox permission profile source is invalid"
                else (
                (item["loc"][0],)
                if item["loc"]
                and isinstance(item["loc"][0], str)
                and item["loc"][0] in known_fields
                else ("<redacted>",)
                )
            ),
            "input": "<redacted>",
            "ctx": {"error": ValueError("input is invalid")},
        }
        for item in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    ]
    return ValidationError.from_exception_data(model.__name__, safe_errors)


class SetupModel(WorkflowModel):
    """Workflow model whose public validation failures never echo input values."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        hide_input_in_errors=True,
    )

    def __init__(self, **data: Any) -> None:
        sanitized_error: ValidationError | None = None
        try:
            super().__init__(**data)
        except ValidationError as error:
            sanitized_error = _sanitized_validation_error(type(self), error)
        if sanitized_error is not None:
            data.clear()
            raise sanitized_error

    def __setattr__(self, name: str, value: Any) -> None:
        sanitized_error: ValidationError | None = None
        try:
            super().__setattr__(name, value)
        except ValidationError as error:
            sanitized_error = _sanitized_validation_error(type(self), error)
        if sanitized_error is not None:
            value = None
            raise sanitized_error

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> Any:
        sanitized_error: ValidationError | None = None
        try:
            result = super().model_validate(obj, *args, **kwargs)
        except ValidationError as error:
            sanitized_error = _sanitized_validation_error(cls, error)
        if sanitized_error is not None:
            obj = None
            args = ()
            kwargs = {}
            raise sanitized_error
        return result

    @classmethod
    def model_validate_json(cls, json_data: Any, *args: Any, **kwargs: Any) -> Any:
        sanitized_error: ValidationError | None = None
        try:
            result = super().model_validate_json(json_data, *args, **kwargs)
        except ValidationError as error:
            sanitized_error = _sanitized_validation_error(cls, error)
        if sanitized_error is not None:
            json_data = None
            args = ()
            kwargs = {}
            raise sanitized_error
        return result

    @classmethod
    def model_validate_strings(cls, obj: Any, *args: Any, **kwargs: Any) -> Any:
        sanitized_error: ValidationError | None = None
        try:
            result = super().model_validate_strings(obj, *args, **kwargs)
        except ValidationError as error:
            sanitized_error = _sanitized_validation_error(cls, error)
        if sanitized_error is not None:
            obj = None
            args = ()
            kwargs = {}
            raise sanitized_error
        return result


def _validated_text(value: str, field_name: str, *, maximum: int = 4096) -> str:
    if not value or value != value.strip():
        raise ValueError(f"{field_name} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError(f"{field_name} is invalid") from None
    if (
        len(value) > maximum
        or len(encoded) > maximum * 4
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROLS
            or character in _BIDI_CONTROL_CHARACTERS
            for character in value
        )
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _validated_url(
    value: str, field_name: str, *, schemes: frozenset[str]
) -> str:
    _validated_text(value, field_name)
    if any(character.isspace() for character in value) or "\\" in value:
        raise ValueError(f"{field_name} must be a safe credential-free URL")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"{field_name} must be a safe credential-free URL") from None
    if (
        parsed.scheme not in schemes
        or _canonical_host(parsed.hostname) is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise ValueError(f"{field_name} must be a safe credential-free URL")
    return value


def _canonical_host(value: str | None) -> str | None:
    if value is None or not value:
        return None
    try:
        return ipaddress.ip_address(value).compressed.casefold()
    except ValueError:
        pass
    try:
        ascii_value = value.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return None
    if len(ascii_value) > 253 or ascii_value.endswith("."):
        return None
    labels = ascii_value.split(".")
    if not all(
        len(label) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        is not None
        for label in labels
    ):
        return None
    return ascii_value


def _validated_absolute_path(value: Path | None, field_name: str) -> Path | None:
    if value is not None:
        _validated_text(str(value), field_name)
        if not value.is_absolute():
            raise ValueError(f"{field_name} must be absolute")
    return value


class RuntimePublicConfig(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    ones_base_url: StrictStr
    ones_team_id: StrictStr
    ones_issue_type_id: StrictStr
    ones_comment_list_path_template: StrictStr
    provider_host: StrictStr
    provider_api_url: StrictStr
    git_author_name: StrictStr
    git_author_email: StrictStr
    codex_auth_mode: Literal["credential", "file"]
    codex_home: Path | None = None
    coding_agent: Literal["codex", "claude"] = "codex"

    @field_validator(
        "ones_team_id",
        "ones_issue_type_id",
        "git_author_name",
    )
    @classmethod
    def validate_plain_text(cls, value: str, info: Any) -> str:
        return _validated_text(value, info.field_name, maximum=320)

    @field_validator("ones_base_url")
    @classmethod
    def validate_ones_url(cls, value: str) -> str:
        return _validated_url(
            value,
            "ones_base_url",
            schemes=frozenset({"http", "https"}),
        )

    @field_validator("provider_api_url")
    @classmethod
    def validate_provider_url(cls, value: str) -> str:
        return _validated_url(
            value,
            "provider_api_url",
            schemes=frozenset({"https"}),
        )

    @field_validator("provider_host")
    @classmethod
    def validate_provider_host(cls, value: str) -> str:
        _validated_text(value, "provider_host", maximum=253)
        if _canonical_host(value) != value:
            raise ValueError("provider_host must be a bare host name")
        return value

    @field_validator("ones_comment_list_path_template")
    @classmethod
    def validate_comment_path_template(cls, value: str) -> str:
        _validated_text(value, "ones_comment_list_path_template")
        parsed = urlsplit(value)
        if (
            not value.startswith("/")
            or value.startswith("//")
            or any(character.isspace() for character in value)
            or "\\" in value
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or any(part in {"", ".", ".."} for part in value[1:].split("/"))
            or set(re.findall(r"\{([^{}]+)\}", value))
            != {"team_id", "item_id"}
            or re.sub(r"\{(?:team_id|item_id)\}", "", value).find("{") != -1
            or re.sub(r"\{(?:team_id|item_id)\}", "", value).find("}") != -1
        ):
            raise ValueError("ones_comment_list_path_template must be an absolute URL path")
        return value

    @field_validator("git_author_email")
    @classmethod
    def validate_git_author_email(cls, value: str) -> str:
        _validated_text(value, "git_author_email", maximum=320)
        if re.fullmatch(r"[^@\s<>]+@[^@\s<>]+", value) is None:
            raise ValueError("git_author_email is invalid")
        return value

    @field_validator("codex_home")
    @classmethod
    def validate_codex_home(cls, value: Path | None) -> Path | None:
        return _validated_absolute_path(value, "codex_home")

    @model_validator(mode="after")
    def validate_provider_host_binding(self) -> RuntimePublicConfig:
        provider_url = urlsplit(self.provider_api_url)
        if not provider_api_host_matches(self.provider_host, provider_url.hostname):
            raise ValueError("provider_api_url host must match provider_host")
        return self


class OnesProbePublicConfig(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    base_url: StrictStr
    team_id: StrictStr
    issue_type_id: StrictStr

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _validated_url(value, "base_url", schemes=frozenset({"http", "https"}))

    @field_validator("team_id", "issue_type_id")
    @classmethod
    def validate_identifiers(cls, value: str, info: Any) -> str:
        return _validated_text(value, info.field_name, maximum=320)


class ProviderProbePublicConfig(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    host: StrictStr
    api_url: StrictStr
    provider: Literal["github", "gitlab"]

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        _validated_text(value, "host", maximum=253)
        if _canonical_host(value) != value:
            raise ValueError("host is invalid")
        return value

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, value: str) -> str:
        return _validated_url(value, "api_url", schemes=frozenset({"https"}))

    @model_validator(mode="after")
    def validate_binding(self) -> ProviderProbePublicConfig:
        if not provider_api_host_matches(self.host, urlsplit(self.api_url).hostname):
            raise ValueError("provider endpoint is invalid")
        return self


class WorkflowDraft(SetupModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, hide_input_in_errors=True
    )

    run_root: Path | None = None
    mirror_root: Path | None = None
    worktree_root: Path | None = None
    sandbox_permission_profile: StrictStr | None = None
    sandbox_permission_profile_source: SandboxPermissionProfileSource = (
        SandboxPermissionProfileSource.MANAGED
    )
    max_codex_attempts: StrictInt = Field(default=3, ge=1, le=10)
    max_baseline_refreshes: StrictInt = Field(default=3, ge=0, le=5)
    tui_max_concurrency: StrictInt = Field(default=3, ge=1, le=8)
    repositories: tuple[RepositoryMapping, ...] = Field(default_factory=tuple)
    repository_groups: tuple[RepositoryGroupMapping, ...] = Field(default_factory=tuple)
    workspace_names: dict[str, str] = Field(default_factory=dict)
    publishing: PublishingConfig | None = None
    verification_nodes: tuple["VerificationNode", ...] = Field(default=())

    @field_validator("workspace_names")
    @classmethod
    def validate_workspace_names(cls, names: dict[str, str]) -> dict[str, str]:
        return DeveloperWorkflowConfig.validate_workspace_names(names)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {
            "sandbox_permission_profile",
            "sandbox_permission_profile_source",
        }:
            profile = (
                value
                if name == "sandbox_permission_profile"
                else self.sandbox_permission_profile
            )
            source = (
                value
                if name == "sandbox_permission_profile_source"
                else self.sandbox_permission_profile_source
            )
            invalid_provenance = False
            try:
                DeveloperWorkflowConfig.validate_sandbox_permission_profile_binding(
                    profile,
                    source,
                )
            except ValueError:
                invalid_provenance = True
            if invalid_provenance:
                value = None
                profile = None
                source = SandboxPermissionProfileSource.MANAGED
                raise _profile_source_validation_error(type(self))
        super().__setattr__(name, value)

    @classmethod
    def model_validate_strings(cls, obj: Any, *args: Any, **kwargs: Any) -> Any:
        invalid_provenance = False
        if isinstance(obj, Mapping):
            try:
                DeveloperWorkflowConfig.validate_sandbox_permission_profile_binding(
                    obj.get("sandbox_permission_profile"),
                    obj.get(
                        "sandbox_permission_profile_source",
                        SandboxPermissionProfileSource.MANAGED,
                    ),
                )
            except ValueError:
                invalid_provenance = True
        if invalid_provenance:
            obj = None
            args = ()
            kwargs = {}
            raise _profile_source_validation_error(cls)
        return super().model_validate_strings(obj, *args, **kwargs)

    @field_validator("run_root", "mirror_root", "worktree_root")
    @classmethod
    def validate_absolute_paths(cls, value: Path | None, info: Any) -> Path | None:
        return _validated_absolute_path(value, info.field_name)

    @field_validator("sandbox_permission_profile")
    @classmethod
    def validate_sandbox_permission_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return DeveloperWorkflowConfig.validate_sandbox_permission_profile(value)

    @field_validator("sandbox_permission_profile_source")
    @classmethod
    def validate_sandbox_permission_profile_source(
        cls, value: SandboxPermissionProfileSource, info: Any
    ) -> SandboxPermissionProfileSource:
        DeveloperWorkflowConfig.validate_sandbox_permission_profile_binding(
            info.data.get("sandbox_permission_profile"), value
        )
        return value

    @model_validator(mode="after")
    def validate_sandbox_permission_profile_binding(self) -> WorkflowDraft:
        DeveloperWorkflowConfig.validate_sandbox_permission_profile_binding(
            self.sandbox_permission_profile,
            self.sandbox_permission_profile_source,
        )
        return self


class SetupDraft(SetupModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, hide_input_in_errors=True
    )

    runtime: RuntimePublicConfig | None = None
    workflow: WorkflowDraft = Field(default_factory=WorkflowDraft)
    detected_secret_kinds: tuple[SecretKind, ...] = Field(default_factory=tuple)


class _CommittedRepositoryMapping(RepositoryMapping):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class _CommittedRepositoryGroupMapping(RepositoryGroupMapping):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    repositories: tuple[_CommittedRepositoryMapping, ...] = Field(min_length=1)


class _CommittedPublishingConfig(PublishingConfig):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class _CommittedDeveloperWorkflowConfig(DeveloperWorkflowConfig):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    max_codex_attempts: StrictInt = Field(ge=1, le=10)
    tui_max_concurrency: StrictInt = Field(default=3, ge=1, le=8)
    repositories: tuple[_CommittedRepositoryMapping, ...] = Field(default_factory=tuple)
    repository_groups: tuple[_CommittedRepositoryGroupMapping, ...] = Field(
        default_factory=tuple
    )
    publishing: _CommittedPublishingConfig

    @field_validator("run_root", "mirror_root", "worktree_root")
    @classmethod
    def validate_committed_paths(cls, value: Path, info: Any) -> Path:
        validated = _validated_absolute_path(value, info.field_name)
        assert validated is not None
        return validated


class ActiveSetup(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    generation: StrictStr
    runtime: RuntimePublicConfig
    workflow: _CommittedDeveloperWorkflowConfig
    credential_kinds: tuple[SecretKind, ...] = Field(min_length=1)

    @field_validator("generation")
    @classmethod
    def validate_generation(cls, value: str) -> str:
        _validated_text(value, "generation", maximum=32)
        if re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise ValueError("generation must be 32 lowercase hexadecimal characters")
        return value

    @field_validator("credential_kinds", mode="before")
    @classmethod
    def validate_strict_credential_kinds(cls, value: object) -> object:
        if (
            not isinstance(value, (tuple, list))
            or not value
            or any(
                type(kind) is not SecretKind
                and (type(kind) is not str or kind not in SecretKind._value2member_map_)
                for kind in value
            )
            or len(set(value)) != len(value)
        ):
            raise ValueError("credential_kinds must be nonempty and unique")
        return value

    @field_validator("workflow", mode="before")
    @classmethod
    def copy_workflow_for_commit(cls, value: object) -> object:
        if isinstance(value, DeveloperWorkflowConfig):
            return value.model_dump(mode="python", round_trip=True)
        return value


class SetupDocument(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    profile_id: StrictStr
    active: ActiveSetup | None = None
    previous: ActiveSetup | None = None
    activation_owner_generation: StrictStr | None = None
    draft: SetupDraft = Field(default_factory=SetupDraft)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_stable_document(cls, value: object) -> object:
        if isinstance(value, dict) and "activation_owner_generation" not in value:
            normalized = dict(value)
            normalized["previous"] = None
            normalized["activation_owner_generation"] = None
            return normalized
        return value

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_strict_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, value: str) -> str:
        _validated_text(value, "profile_id", maximum=128)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
            raise ValueError("profile_id is invalid")
        return value

    @field_validator("activation_owner_generation")
    @classmethod
    def validate_activation_owner_generation(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _validated_text(value, "activation_owner_generation", maximum=32)
        if re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise ValueError(
                "activation_owner_generation must be 32 lowercase hexadecimal characters"
            )
        return value

    @model_validator(mode="after")
    def validate_distinct_generations(self) -> SetupDocument:
        if (
            self.active is not None
            and self.previous is not None
            and self.active.generation == self.previous.generation
        ):
            raise ValueError("active and previous generations must differ")
        if self.activation_owner_generation is None:
            if self.previous is not None:
                raise ValueError("stable configuration cannot retain previous activation")
        elif (
            self.active is None
            or self.active.generation != self.activation_owner_generation
        ):
            raise ValueError("activation owner must match active generation")
        return self


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeSecrets:
    values: Mapping[SecretKind, str] = field(repr=False)

    def __post_init__(self) -> None:
        copied: dict[SecretKind, str] = {}
        for kind, value in self.values.items():
            if type(kind) is not SecretKind or type(value) is not str:
                raise SetupValidationError("runtime credential is invalid")
            copied[kind] = value
        object.__setattr__(self, "values", MappingProxyType(copied))

    def require(self, kind: SecretKind) -> str:
        value = self.values.get(kind, "")
        if not value:
            raise SetupValidationError("runtime credential is unavailable")
        return value


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeInputs:
    public: RuntimePublicConfig
    secrets: RuntimeSecrets = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.public) is not RuntimePublicConfig:
            raise TypeError("public must be RuntimePublicConfig")
        if type(self.secrets) is not RuntimeSecrets:
            raise TypeError("secrets must be RuntimeSecrets")


__all__ = [
    "ActiveSetup",
    "DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE",
    "OnesProbePublicConfig",
    "ProviderProbePublicConfig",
    "RuntimeInputs",
    "RuntimePublicConfig",
    "RuntimeSecrets",
    "SecretKind",
    "SetupDocument",
    "SetupDraft",
    "SetupValidationError",
    "WorkflowDraft",
]
