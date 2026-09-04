"""Exact provider/API host bindings; never accept arbitrary cross-host tokens."""
from __future__ import annotations


def provider_api_host_matches(host: str, api_host: str | None) -> bool:
    if not api_host:
        return False
    pair = (host.casefold(), api_host.casefold())
    return pair[0] == pair[1] or pair == ("github.com", "api.github.com")
