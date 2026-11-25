"""
Authentication and License Key validation module.
Supports:
  - Header: X-API-Key or Authorization: Bearer <token>
  - Polar.sh license keys (arca_...) with in-memory TTL caching
  - Master API Key bypass for administration & testing
"""

from __future__ import annotations

import time
from typing import Optional
import httpx
import structlog
from fastapi import Header, HTTPException, status

from .config import get_settings

log = structlog.get_logger()

# In-memory cache for validated keys: {key: (is_valid, expire_timestamp)}
_KEY_CACHE: dict[str, tuple[bool, float]] = {}
_CACHE_TTL_SECONDS = 600  # 10 minutes cache to prevent hitting Polar on every request


async def verify_polar_license_key(key: str, org_id: str, access_token: str) -> bool:
    """Validate license key against Polar.sh API."""
    now = time.time()
    if key in _KEY_CACHE:
        is_valid, exp = _KEY_CACHE[key]
        if now < exp:
            return is_valid

    url = "https://api.polar.sh/v1/customer-portal/license-keys/validate"
    payload = {"key": key, "organization_id": org_id}
    headers = {"Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                # Polar status "granted" indicates valid active license
                is_valid = data.get("status") in ("granted", "active")
                _KEY_CACHE[key] = (is_valid, now + _CACHE_TTL_SECONDS)
                return is_valid
            else:
                log.warning("polar_validation_failed", status=resp.status_code, body=resp.text)
                return False
    except Exception as exc:
        log.error("polar_connection_error", error=str(exc))
        # Fail safe: if Polar is temporarily unreachable and key matches prefix, allow with warning
        if key.startswith("arca_"):
            return True
        return False


async def get_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> str:
    """
    FastAPI dependency to extract and validate the API key.
    Accepts:
      - Header 'X-API-Key: <key>'
      - Header 'Authorization: Bearer <key>'
    """
    s = get_settings()

    # Extract token
    token = x_api_key
    if not token and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        else:
            token = authorization

    # If auth requirement is turned off (e.g. testing or local dev)
    if not s.require_api_key:
        return token or "anonymous_dev"

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "API_KEY_REQUIRED",
                "message": (
                    "Se requiere una API Key para utilizar este servicio. "
                    f"Puedes obtener tu clave de acceso en {s.polar_checkout_url}"
                ),
                "checkout_url": s.polar_checkout_url,
                "billed": False,
            },
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # 1. Master Key check (admin / tests)
    if s.master_api_key and token == s.master_api_key:
        return token

    # 2. Configured valid API keys check
    if s.valid_api_keys:
        valid_list = [k.strip() for k in s.valid_api_keys.split(",") if k.strip()]
        if token in valid_list:
            return token

    # 3. Polar.sh license key check
    if s.polar_organization_id:
        is_valid = await verify_polar_license_key(token, s.polar_organization_id, s.polar_access_token)
        if is_valid:
            return token
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_API_KEY",
                "message": (
                    "La API Key provista es inválida o ha expirado. "
                    f"Renueva tu suscripción o adquiere un pack en {s.polar_checkout_url}"
                ),
                "checkout_url": s.polar_checkout_url,
                "billed": False,
            },
        )

    # 4. Fallback if Polar Org ID is not yet set in environment:
    # Accept keys starting with arca_ or test_ to avoid blocking valid buyers
    if token.startswith("arca_") or token.startswith("test_"):
        return token

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "INVALID_API_KEY",
            "message": (
                "API Key no reconocida. "
                f"Obtén tu clave de producción en {s.polar_checkout_url}"
            ),
            "checkout_url": s.polar_checkout_url,
            "billed": False,
        },
    )
