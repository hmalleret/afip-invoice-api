"""
Application configuration and circuit breaker setup.
"""

from __future__ import annotations

import os
from functools import lru_cache

import pybreaker
from pydantic_settings import BaseSettings, SettingsConfigDict


from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Google AI
    google_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash-exp"
    gemini_max_retries: int = 3
    gemini_timeout_seconds: int = 30

    # API
    api_title: str = "AFIP Invoice Extraction API"
    api_version: str = "1.0.0"
    max_content_size_mb: int = 10
    request_timeout_seconds: int = 60

    # Circuit breaker: open after 3 consecutive failures, try again after 60s
    cb_fail_max: int = 3
    cb_reset_timeout: int = 60

    # Billing: don't charge if confidence below this threshold
    min_confidence_to_bill: float = 0.50

    # RapidAPI header for identifying callers
    rapidapi_proxy_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Circuit breakers — one per upstream dependency
# ---------------------------------------------------------------------------

_gemini_cb = None


def get_gemini_circuit_breaker() -> pybreaker.CircuitBreaker:
    global _gemini_cb
    if _gemini_cb is None:
        s = get_settings()
        _gemini_cb = pybreaker.CircuitBreaker(
            fail_max=s.cb_fail_max,
            reset_timeout=s.cb_reset_timeout,
            name="gemini",
        )
    return _gemini_cb
