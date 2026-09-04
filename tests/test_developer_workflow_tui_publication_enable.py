from __future__ import annotations

from types import SimpleNamespace
import httpx
import pytest

from src.developer_workflow.pr_provider import HttpPullRequestClient
from src.developer_workflow.provider_endpoints import provider_api_host_matches
from src.developer_workflow.setup_models import ProviderProbePublicConfig
from src.developer_workflow.setup_validation import ProviderProbeInput
from src.developer_workflow.tui.controller import TuiController, PublicationConfigurationError


def test_github_public_api_and_web_identity_remain_separate():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[])
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = HttpPullRequestClient(provider="github", provider_host="github.com",
            api_base_url="https://api.github.com", token_provider=lambda: "test-token", client=transport)
        client.validate_credentials()
        assert client.find(repo_url="https://github.com/org/repo.git", head="branch", base="main", marker="marker") is None
        assert requests[0].url.host == "api.github.com"
        assert requests[0].headers["Authorization"] == "Bearer test-token"
        ProviderProbeInput(host="github.com", api_url="https://api.github.com")
        ProviderProbePublicConfig(host="github.com", api_url="https://api.github.com", provider="github")


@pytest.mark.parametrize("host", ["attacker.test", "api.github.com.attacker.test", "github.com.attacker.test"])
def test_provider_endpoint_does_not_allow_arbitrary_cross_host(host):
    assert not provider_api_host_matches("github.com", host)
    with pytest.raises(ValueError):
        ProviderProbeInput(host="github.com", api_url=f"https://{host}")


def test_missing_token_blocks_before_approval_or_git_mutation():
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected network"))) as transport:
        client = HttpPullRequestClient(provider="gitlab", provider_host="git.example.test",
            api_base_url="https://git.example.test/api/v4", token_provider=lambda: "", client=transport)
        orchestrator = SimpleNamespace(publisher=SimpleNamespace(pr_client=client))
        controller = TuiController(orchestrator, object())
        try:
            with pytest.raises(PublicationConfigurationError, match="配置 PR/MR"):
                controller.prepare_action("run", "approve")
            with pytest.raises(PublicationConfigurationError):
                controller.approve(None, "member")
            with pytest.raises(PublicationConfigurationError):
                controller.resume_publication(None)
        finally:
            controller.close()


def test_local_git_mode_reads_user_config_without_leaking_environment(tmp_path, monkeypatch):
    import os
    import subprocess
    from src.developer_workflow.repository import WorktreeRepository
    home = tmp_path / "user-home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n name = Local Test User\n email = local@example.test\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "ignored-injected-path")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "9")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent"))
    monkeypatch.setenv("SECRET_CANARY", "not-for-git")
    repository = WorktreeRepository(tmp_path / "mirrors", tmp_path / "worktrees", use_local_git_config=True)
    repository.identity_env_provider = lambda: {"GIT_AUTHOR_NAME": "must-not-override"}
    env = repository._git_environment()
    assert "GIT_CONFIG_GLOBAL" not in env
    assert "GIT_CONFIG_NOSYSTEM" not in env
    assert "GIT_AUTHOR_NAME" not in env
    assert "SECRET_CANARY" not in env
    assert "GIT_SSH_COMMAND" not in env
    assert env["SSH_AUTH_SOCK"] == str(tmp_path / "agent")
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    result = subprocess.run(["git", "config", "--global", "user.name"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "Local Test User"
    assert os.environ["GIT_CONFIG_GLOBAL"] == "ignored-injected-path"
