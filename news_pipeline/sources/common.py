from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any

import requests

from ..models import RawArtifact
from ..storage import NewsDataStore


DEFAULT_USER_AGENT = "Real-Stock-Analyzer/1.0 (local research archive)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CachedHttpClient:
    def __init__(
        self,
        store: NewsDataStore,
        *,
        session: requests.Session | None = None,
        timeout: float = 15.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.store = store
        self.session = session or requests.Session()
        self.timeout = timeout
        self.user_agent = user_agent

    def get_bytes(
        self,
        source: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        suffix: str = "bin",
        content_type: str = "application/octet-stream",
        query: str = "",
        cache_ttl: timedelta = timedelta(minutes=20),
    ) -> tuple[bytes, RawArtifact]:
        key = self.store.cache_key(source, url, params)
        cached = self.store.cache_get(key)
        if cached is not None:
            return self.store.read_artifact(cached), cached
        request_headers = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}
        request_headers.update(headers or {})
        response = self.session.get(url, params=params, headers=request_headers, timeout=self.timeout)
        response.raise_for_status()
        payload = bytes(response.content)
        artifact = self.store.store_raw(
            source,
            payload,
            suffix=suffix,
            content_type=response.headers.get("Content-Type", content_type) if hasattr(response, "headers") else content_type,
            # Store the endpoint, never the fully expanded response URL: query
            # parameters may contain OpenDART credentials.
            request_url=url,
            query=query,
        )
        self.store.cache_put(key, artifact, ttl=cache_ttl)
        return payload, artifact

    def get_json(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any], RawArtifact]:
        kwargs.setdefault("suffix", "json")
        kwargs.setdefault("content_type", "application/json")
        payload, artifact = self.get_bytes(*args, **kwargs)
        value = json.loads(payload.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("JSON 응답의 최상위 값이 객체가 아닙니다.")
        return value, artifact
