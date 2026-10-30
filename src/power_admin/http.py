"""Thin HTTP client with bearer auth, retries on throttling, and paging helpers."""

from __future__ import annotations

import time
from typing import Any, Iterator

import requests

from .auth import TokenProvider


class ApiError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        self.status = status
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")


class ApiClient:
    def __init__(
        self,
        tokens: TokenProvider,
        session: requests.Session | None = None,
        max_retries: int = 5,
        sleep=time.sleep,
    ):
        self.tokens = tokens
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self._sleep = sleep

    def request(self, method: str, url: str, scope: str, **kwargs: Any) -> requests.Response:
        headers = {"Authorization": f"Bearer {self.tokens.get_token(scope)}", "Accept": "application/json"}
        headers.update(kwargs.pop("headers", {}) or {})
        for attempt in range(self.max_retries + 1):
            resp = self.session.request(method, url, headers=headers, timeout=120, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                self._sleep(min(delay, 60))
                continue
            if resp.status_code >= 400:
                raise ApiError(resp.status_code, url, resp.text)
            return resp
        raise AssertionError("unreachable")

    def get_json(self, url: str, scope: str, **kwargs: Any) -> Any:
        resp = self.request("GET", url, scope, **kwargs)
        return resp.json() if resp.content else None

    def post_json(self, url: str, scope: str, body: Any) -> Any:
        resp = self.request("POST", url, scope, json=body)
        return resp.json() if resp.content else None

    def iter_odata(self, url: str, scope: str, params: dict | None = None) -> Iterator[dict]:
        """Follow @odata.nextLink (Graph and most Power Platform list endpoints)."""
        next_url: str | None = url
        while next_url:
            page = self.get_json(next_url, scope, params=params) or {}
            params = None  # nextLink already carries the query string
            yield from page.get("value", [])
            next_url = page.get("@odata.nextLink")
