"""Validate bearer tokens with Supabase Auth before establishing database identity."""

import os
from urllib.parse import urlparse
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx

from .contracts import BackendUnavailable
from .database import load_environment

bearer = HTTPBearer(auto_error=False)


def verify_access_token(token: str, *, transport=None) -> UUID:
    load_environment()
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    parsed = urlparse(url)
    if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".supabase.co")
            or "PROJECT_REF" in url or parsed.path or parsed.query or parsed.fragment
            or parsed.username or parsed.password or not key.startswith("sb_publishable_")):
        raise BackendUnavailable("Configure this project's Supabase URL and publishable key for authentication.")
    try:
        with httpx.Client(timeout=8, follow_redirects=False, transport=transport) as client:
            response = client.get(url + "/auth/v1/user",
                                  headers={"apikey": key, "Authorization": f"Bearer {token}"})
        if response.status_code in (401, 403):
            raise HTTPException(401, "Invalid or expired Supabase access token.", headers={"WWW-Authenticate": "Bearer"})
        if response.status_code != 200:
            raise BackendUnavailable("Supabase authentication is temporarily unavailable.")
        payload = response.json()
        if payload.get("role") != "authenticated":
            raise HTTPException(401, "An authenticated user access token is required.")
        return UUID(payload["id"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise BackendUnavailable("Supabase authentication could not be verified.") from None


def require_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> UUID:
    if credentials is None:
        raise HTTPException(401, "Sign in with Supabase and supply a bearer access token.",
                            headers={"WWW-Authenticate": "Bearer"})
    return verify_access_token(credentials.credentials)
