# auth.py
"""
Laravel Sanctum bearer token validator.
Used as a FastAPI dependency via HTTPBearer security scheme.
"""

import hashlib
import logging
import os
from typing import Optional

import httpx
from cachetools import TTLCache
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LARAVEL_AUTH_URL: str = os.getenv(
    "LARAVEL_AUTH_URL",
    "https://quantumpalate.com/api/v1/currentUser",
)
AUTH_CACHE_TTL: int = int(os.getenv("AUTH_CACHE_TTL", "60"))
AUTH_TIMEOUT: float = float(os.getenv("AUTH_TIMEOUT", "5.0"))
AUTH_CACHE_MAX_SIZE: int = 10_000

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------
_user_cache: TTLCache = TTLCache(maxsize=AUTH_CACHE_MAX_SIZE, ttl=AUTH_CACHE_TTL)
_http_client = httpx.AsyncClient(timeout=AUTH_TIMEOUT)

# HTTPBearer tells Swagger to show the "Authorize" button and attach the
# Authorization header automatically to every request.
# auto_error=False lets us customize the 401 response instead of FastAPI's default.
_bearer_scheme = HTTPBearer(auto_error=False, description="Laravel Sanctum bearer token")


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def require_bearer(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    """
    FastAPI dependency: validates the Authorization header against Laravel.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials.strip()
    key = _cache_key(token)

    cached = _user_cache.get(key)
    if cached is not None:
        return cached

    try:
        response = await _http_client.get(
            LARAVEL_AUTH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
    except httpx.RequestError as e:
        logger.exception("Auth upstream unreachable: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        )

    if response.status_code == status.HTTP_401_UNAUTHORIZED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if response.status_code != status.HTTP_200_OK:
        logger.error(
            "Unexpected auth response from Laravel: %s %s",
            response.status_code,
            response.text[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Authentication service error",
        )

    try:
        user = response.json()
    except ValueError:
        logger.error("Laravel returned non-JSON response")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Authentication service returned invalid response",
        )

    _user_cache[key] = user
    return user


async def shutdown_auth() -> None:
    """Close the HTTP client cleanly on server shutdown."""
    await _http_client.aclose()