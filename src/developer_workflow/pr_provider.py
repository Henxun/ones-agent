"""Strict GitHub/GitLab pull-request provider adapters."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote, urlsplit
import re

import httpx

from .contracts import validate_git_ref_name
from .provider_endpoints import provider_api_host_matches


class PullRequestProviderError(RuntimeError):
    """A redacted provider-boundary failure."""


def parse_repository_identity(
    repo_url: str, expected_host: str | None
) -> tuple[str, str]:
    """Parse a repository URL and optionally bind it to a publishing host.

    Analysis-only runtimes still validate the URL shape, credentials, and path,
    but do not need to pretend that every local source publishes through the
    globally configured provider.  Publishing callers continue to pass a host
    and therefore retain the strict identity check.
    """

    if type(repo_url) is not str or not repo_url.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in repo_url
    ):
        raise PullRequestProviderError("Repository URL is invalid")
    value = repo_url.strip()
    scp = re.fullmatch(r"(?:[^@/:\s]+@)?([^/:\s]+):(.+)", value)
    if scp and "://" not in value:
        host, path = scp.group(1).casefold(), scp.group(2)
    else:
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise PullRequestProviderError("Repository URL is invalid") from None
        if parsed.scheme not in {"https", "http", "ssh"}:
            raise PullRequestProviderError("Repository URL scheme is invalid")
        if parsed.query or parsed.fragment or parsed.hostname is None:
            raise PullRequestProviderError("Repository URL is invalid")
        if parsed.scheme in {"http", "https"} and (
            parsed.username is not None or parsed.password is not None
        ):
            raise PullRequestProviderError("Repository URL credentials are forbidden")
        if parsed.password is not None:
            raise PullRequestProviderError("Repository URL credentials are forbidden")
        host, path = parsed.hostname.casefold(), parsed.path
    if expected_host is not None and host != expected_host.casefold():
        raise PullRequestProviderError("Repository host does not match provider")
    if "?" in path or "#" in path or "\\" in path:
        raise PullRequestProviderError("Repository path is invalid")
    parts = path.removesuffix(".git").strip("/").split("/")
    if len(parts) < 2 or any(not part or part in {".", ".."} for part in parts):
        raise PullRequestProviderError("Repository path is invalid")
    return "/".join(parts[:-1]), parts[-1]


def _safe_text(value: str, label: str, *, allow_newline: bool = False) -> str:
    if type(value) is not str or not value.strip():
        raise PullRequestProviderError(f"{label} is invalid")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise PullRequestProviderError(f"{label} is invalid") from None
    allowed = "\n\t" if allow_newline else ""
    if any(ord(ch) < 32 and ch not in allowed for ch in value):
        raise PullRequestProviderError(f"{label} is invalid")
    return value


@dataclass(slots=True)
class HttpPullRequestClient:
    provider: str
    provider_host: str
    api_base_url: str
    token_provider: Callable[[], str]
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30.0), repr=False)
    retry_backoff: Callable[[int], float] = field(default=lambda attempt: 0.2 * 2 ** (attempt - 1), repr=False)
    max_attempts: int = 3
    max_pages: int = 10

    def __post_init__(self) -> None:
        if self.provider not in {"github", "gitlab"}:
            raise ValueError("provider must be github or gitlab")
        if not (1 <= self.max_attempts <= 5 and 1 <= self.max_pages <= 50):
            raise ValueError("provider retry/page limits are invalid")
        parsed = urlsplit(self.api_base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or not provider_api_host_matches(self.provider_host, parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("provider API URL must be HTTPS on the configured host")
        self.api_base_url = self.api_base_url.rstrip("/")

    def _repository(self, repo_url: str) -> tuple[str, str]:
        return parse_repository_identity(repo_url, self.provider_host)

    def _headers(self) -> dict[str, str]:
        try:
            token = self.token_provider()
        except Exception:
            raise PullRequestProviderError("Provider credential is unavailable") from None
        token = _safe_text(token, "provider credential")
        if self.provider == "github":
            return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        return {"PRIVATE-TOKEN": token}

    def validate_credentials(self) -> None:
        """Fail before Git writes when publication credentials are absent."""
        self._headers()

    def _get(self, path: str, params: dict[str, object]) -> object:
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.get(
                    f"{self.api_base_url}{path}", params=params, headers=self._headers()
                )
            except httpx.HTTPError:
                if attempt == self.max_attempts:
                    raise PullRequestProviderError("PR provider GET failed") from None
                time.sleep(self.retry_backoff(attempt))
                continue
            if response.status_code in {429} or 500 <= response.status_code <= 599:
                if attempt == self.max_attempts:
                    raise PullRequestProviderError("PR provider GET failed")
                time.sleep(self.retry_backoff(attempt))
                continue
            if response.status_code != 200:
                raise PullRequestProviderError(
                    f"PR provider GET failed with status {response.status_code}"
                )
            try:
                return response.json()
            except ValueError:
                raise PullRequestProviderError("PR provider returned malformed JSON") from None
        raise PullRequestProviderError("PR provider GET failed")

    def find(self, *, repo_url: str, head: str, base: str, marker: str,
             draft: bool = False, expected_sha: str = "") -> str | None:
        self._validate_draft_request(draft, expected_sha)
        owner, repo = self._repository(repo_url)
        head = validate_git_ref_name(_safe_text(head, "head branch"))
        base = validate_git_ref_name(_safe_text(base, "base branch"))
        marker = _safe_text(marker, "run marker")
        for page in range(1, self.max_pages + 1):
            if self.provider == "github":
                path = f"/repos/{quote(owner, safe='/')}/{quote(repo, safe='')}/pulls"
                params = {"state":"open", "head":f"{owner}:{head}", "base":base, "per_page":100, "page":page}
            else:
                project = quote(f"{owner}/{repo}", safe="")
                path = f"/api/v4/projects/{project}/merge_requests"
                params = {"state":"opened", "source_branch":head, "target_branch":base, "per_page":100, "page":page}
            payload = self._get(path, params)
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                raise PullRequestProviderError("PR provider list payload is malformed")
            for item in payload:
                if self.provider == "github":
                    item_head = item.get("head", {}).get("ref") if isinstance(item.get("head"), dict) else None
                    item_base = item.get("base", {}).get("ref") if isinstance(item.get("base"), dict) else None
                    body, url = item.get("body"), item.get("html_url")
                else:
                    item_head, item_base = item.get("source_branch"), item.get("target_branch")
                    body, url = item.get("description"), item.get("web_url")
                if item_head == head and item_base == base and isinstance(body, str) and marker in body:
                    if draft:
                        self._assert_draft(item, expected_sha)
                    return self._url(url)
            if len(payload) < 100:
                return None
        raise PullRequestProviderError("PR provider pagination limit was exceeded")

    def create(
        self, *, repo_url: str, head: str, base: str, title: str, body: str, marker: str,
        draft: bool = False, expected_sha: str = ""
    ) -> str:
        self._validate_draft_request(draft, expected_sha)
        owner, repo = self._repository(repo_url)
        head = validate_git_ref_name(_safe_text(head, "head branch"))
        base = validate_git_ref_name(_safe_text(base, "base branch"))
        title = _safe_text(title, "PR title")
        body = _safe_text(body, "PR body", allow_newline=True)
        marker = _safe_text(marker, "run marker")
        description = f"{body}\n\n{marker}"
        if self.provider == "github":
            path = f"/repos/{quote(owner, safe='/')}/{quote(repo, safe='')}/pulls"
            payload = {"title":title, "head":head, "base":base, "body":description}
            if draft:
                payload["draft"] = True
            url_field = "html_url"
        else:
            project = quote(f"{owner}/{repo}", safe="")
            path = f"/api/v4/projects/{project}/merge_requests"
            payload = {"source_branch":head, "target_branch":base, "title":title, "description":description}
            if draft:
                payload["title"] = title if title.startswith("Draft:") else f"Draft: {title}"
            url_field = "web_url"
        try:
            response = self.client.post(
                f"{self.api_base_url}{path}", json=payload, headers=self._headers()
            )
        except httpx.HTTPError:
            raise PullRequestProviderError("PR creation outcome is uncertain") from None
        if response.status_code not in {200, 201}:
            raise PullRequestProviderError(
                f"PR creation failed with status {response.status_code}"
            )
        try:
            result = response.json()
        except ValueError:
            raise PullRequestProviderError("PR creation response is malformed") from None
        if not isinstance(result, dict):
            raise PullRequestProviderError("PR creation response is malformed")
        if draft:
            self._assert_draft(result, expected_sha)
        return self._url(result.get(url_field))

    @staticmethod
    def _validate_draft_request(draft: bool, expected_sha: str) -> None:
        if type(draft) is not bool or (draft and (type(expected_sha) is not str
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_sha) is None)):
            raise PullRequestProviderError("Draft PR requires the approved commit identity")

    def _assert_draft(self, item: dict, expected_sha: str) -> None:
        head = item.get("head")
        sha = head.get("sha") if self.provider == "github" and isinstance(head, dict) else item.get("sha")
        if not expected_sha or sha != expected_sha or item.get("draft") is not True:
            raise PullRequestProviderError("PR must remain draft at the approved commit")

    def set_verification_pending(self, *, repo_url: str, sha: str, branch: str) -> None:
        """Commit-bound pending check. Never report success from PR prose/checkboxes."""
        from .pr_handoff import CHECK_CONTEXT

        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) is None:
            raise PullRequestProviderError("Verification commit identity is invalid")
        branch = validate_git_ref_name(branch)
        owner, repo = self._repository(repo_url)
        if self.provider == "github":
            path = f"/repos/{quote(owner, safe='/')}/{quote(repo, safe='')}/statuses/{sha}"
            payload = {"state": "pending", "context": CHECK_CONTEXT,
                       "description": "External verification required for this commit"}
        else:
            project = quote(f"{owner}/{repo}", safe="")
            path = f"/api/v4/projects/{project}/statuses/{sha}"
            payload = {"state": "pending", "name": CHECK_CONTEXT, "ref": branch,
                       "description": "External verification required for this commit"}
        try:
            response = self.client.post(f"{self.api_base_url}{path}", json=payload, headers=self._headers())
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError):
            raise PullRequestProviderError("External verification pending check could not be confirmed") from None
        state_key = "state" if self.provider == "github" else "status"
        name_key = "context" if self.provider == "github" else "name"
        if not isinstance(result, dict) or result.get(state_key) != "pending" or result.get(name_key) != CHECK_CONTEXT:
            raise PullRequestProviderError("External verification pending check response is invalid")

    def _url(self, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise PullRequestProviderError("PR provider returned an empty URL")
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() != self.provider_host.casefold()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise PullRequestProviderError("PR provider returned an unsafe URL")
        return value.strip()


__all__ = ["HttpPullRequestClient", "PullRequestProviderError", "parse_repository_identity"]
