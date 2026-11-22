"""Optional Upstash REST cache. Supabase remains authoritative on every access check."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import json
import os
import re
from threading import Lock
from time import time, monotonic
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from fastapi.encoders import jsonable_encoder
from .database import load_environment

VERSION = "v1"


def _encode(value):
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Nonfinite cache value.")
        return {"$decimal": str(value)}
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("Unsupported cache value.")


def _decode(value):
    if set(value) == {"$date"}:
        return date.fromisoformat(value["$date"])
    if set(value) == {"$datetime"}:
        return datetime.fromisoformat(value["$datetime"])
    if set(value) == {"$decimal"}:
        result = Decimal(value["$decimal"])
        if not result.is_finite():
            raise ValueError("Nonfinite cache value.")
        return result
    return value


@dataclass(frozen=True)
class CacheRead:
    value: object = None
    status: str = "BYPASS"


def _reject_constant(value):
    raise ValueError("Nonfinite cache value.")


class UpstashCache:
    def __init__(self, url="", token="", *, enabled=False, ttl=120, namespace="payscope:dev",
                 max_bytes=2_000_000, transport=None):
        self._url, self._token = url.rstrip("/"), token
        self.enabled, self.ttl, self.namespace = enabled, ttl, namespace
        self.max_bytes, self._transport = max_bytes, transport
        self._retry_at, self._lock = 0.0, Lock()
        try:
            parsed = urlparse(self._url)
            self.configured = bool(parsed.scheme == "https" and (parsed.hostname or "").endswith(".upstash.io")
                and parsed.port in {None, 443} and not (parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password)
                and token and token.isascii() and not any(c.isspace() for c in token)
                and re.fullmatch(r"[A-Za-z0-9:_-]{1,80}", namespace)
                and isinstance(ttl, int) and 15 <= ttl <= 3600 and 1024 <= max_bytes <= 2_000_000)
        except (ValueError, TypeError):
            self.configured = False

    @classmethod
    def from_environment(cls):
        load_environment()
        try:
            ttl = int(os.environ.get("UPSTASH_CACHE_TTL_SECONDS", "120"))
        except ValueError:
            ttl = 0
        return cls(os.environ.get("UPSTASH_REDIS_REST_URL", ""), os.environ.get("UPSTASH_REDIS_REST_TOKEN", ""),
                   enabled=os.environ.get("UPSTASH_CACHE_ENABLED", "false").lower() == "true", ttl=ttl,
                   namespace=os.environ.get("UPSTASH_CACHE_NAMESPACE", "payscope:dev"))

    @property
    def active(self):
        return self.enabled and self.configured and monotonic() >= self._retry_at

    def status(self):
        state = "DISABLED" if not self.enabled else "NOT_CONFIGURED" if not self.configured else "READY" if self.active else "DEGRADED"
        return {"provider": "upstash", "enabled": self.enabled, "configured": self.configured,
                "state": state, "ttl_seconds": self.ttl, "connection_verified": False}

    def key(self, material):
        canonical = json.dumps(jsonable_encoder(material), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return f"{self.namespace}:{VERSION}:" + sha256(canonical.encode()).hexdigest()

    def _command(self, *command):
        if not self.active:
            return False, None
        try:
            with httpx.Client(timeout=httpx.Timeout(1.0, connect=0.5), follow_redirects=False,
                              transport=self._transport) as client:
                # Commands/values stay in the POST body; credentials stay out of URLs and logs.
                response = client.post(self._url, json=list(command), headers={"Authorization": f"Bearer {self._token}"})
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
                raise ValueError("Unexpected cache response.")
            return True, payload["result"]
        except (httpx.HTTPError, ValueError, TypeError):
            with self._lock:
                self._retry_at = monotonic() + 30
            return False, None

    def read(self, key):
        if not self.active:
            return CacheRead(status=self.status()["state"])
        ok, value = self._command("GET", key)
        if not ok:
            return CacheRead(status="UNAVAILABLE")
        if value is None:
            return CacheRead(status="MISS")
        try:
            if not isinstance(value, str) or len(value.encode()) > self.max_bytes:
                raise ValueError("Oversize cache entry.")
            envelope = json.loads(value, object_hook=_decode, parse_constant=_reject_constant)
            if (not isinstance(envelope, dict) or envelope.get("version") != VERSION or envelope.get("key") != key
                    or not isinstance(envelope.get("expires_at"), (int, float)) or envelope["expires_at"] <= time()
                    or envelope["expires_at"] > time() + self.ttl + 5 or "value" not in envelope):
                raise ValueError("Invalid or stale cache entry.")
            return CacheRead(value=envelope["value"], status="HIT")
        except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
            return CacheRead(status="INVALID")

    def write(self, key, value):
        if not self.active:
            return False
        try:
            envelope = {"version": VERSION, "key": key, "expires_at": time() + self.ttl, "value": value}
            serialized = json.dumps(envelope, default=_encode, allow_nan=False, separators=(",", ":"))
            if len(serialized.encode()) > self.max_bytes:
                return False
        except (ValueError, TypeError):
            return False
        ok, result = self._command("SET", key, serialized, "EX", self.ttl)
        return ok and result == "OK"

    def check(self):
        """Owner CLI probe: synthetic temporary value, expires even if cleanup fails."""
        result = self.status()
        if not self.active:
            return result
        key = self.key({"probe": str(uuid4())})
        written = self.write(key, {"probe": "payscope-cache-check"})
        read = self.read(key) if written else CacheRead()
        verified = read.status == "HIT" and read.value == {"probe": "payscope-cache-check"}
        self._command("DEL", key)
        return {**self.status(), "connection_verified": verified}


@lru_cache(maxsize=1)
def get_cache():
    # Configuration changes take effect on server restart, like database/model settings.
    return UpstashCache.from_environment()
