import hashlib
import json
import logging
from typing import Any

import httpx
from diskcache import Cache

from ...config import settings

log = logging.getLogger("enricher")

_cache = Cache(str(settings.data_dir / "cache" / "http"), size_limit=int(2e9))


class HttpEnricher:
    base_url: str = ""
    user_agent: str = "metadata-gapfixer/0.1"
    cache_ttl: int = 60 * 60 * 24 * 7  # 7 days

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": f"{self.user_agent} (mailto:{settings.contact_email})",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict | list | None:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        key = "GET:" + hashlib.sha256(f"{url}?{json.dumps(params or {}, sort_keys=True)}".encode()).hexdigest()
        cached = _cache.get(key)
        if cached is not None:
            return cached
        try:
            with httpx.Client(timeout=self.timeout, headers=self._headers()) as client:
                resp = client.get(url, params=params)
        except httpx.HTTPError as exc:
            log.warning("%s GET %s failed: %s", self.__class__.__name__, url, exc)
            return None
        if resp.status_code == 404:
            _cache.set(key, None, expire=self.cache_ttl)
            return None
        if resp.status_code >= 400:
            log.warning("%s %s -> %s: %s", self.__class__.__name__, url, resp.status_code, resp.text[:200])
            return None
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            return None
        _cache.set(key, payload, expire=self.cache_ttl)
        return payload
