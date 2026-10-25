"""Configuration and security tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import _matches


# ── settings ──────────────────────────────────────────────────────────────
def test_env_prefix_is_applied(monkeypatch):
    monkeypatch.setenv("DASH_AWS_REGION", "eu-central-1")
    monkeypatch.setenv("DASH_MONTHLY_BUDGET", "12345")
    settings = Settings()
    assert settings.aws_region == "eu-central-1"
    assert settings.monthly_budget == 12345.0


def test_cors_origins_are_split_and_trimmed():
    settings = Settings(cors_origins="http://a.com, http://b.com ,")
    assert settings.cors_origin_list == ["http://a.com", "http://b.com"]


def test_invalid_environment_is_rejected():
    with pytest.raises(ValidationError):
        Settings(environment="danger-zone")


def test_invalid_provider_mode_is_rejected():
    with pytest.raises(ValidationError):
        Settings(provider_mode="magic")


def test_public_fingerprint_excludes_secrets():
    settings = Settings(api_key="super-secret-value")
    fingerprint = settings.public_fingerprint()
    assert "super-secret-value" not in str(fingerprint)
    assert "api_key" not in fingerprint
    assert fingerprint["currency"] == "USD"


def test_api_key_injection_is_refused_in_production():
    production = Settings(environment="production", bootstrap_dashboard=True)
    assert production.api_key_injected is False

    local = Settings(environment="local", bootstrap_dashboard=True)
    assert local.api_key_injected is True


def test_api_key_injection_can_be_disabled():
    settings = Settings(environment="local", bootstrap_dashboard=False)
    assert settings.api_key_injected is False


def test_auth_can_be_disabled():
    settings = Settings(auth_enabled=False, api_key="anything")
    assert settings.effective_api_key is None


# ── constant-time comparison ──────────────────────────────────────────────
def test_compare_digest_matching():
    assert _matches("abc123", "abc123") is True


@pytest.mark.parametrize("candidate", ["abc124", "abc", "", "ABC123", "abc1234"])
def test_compare_digest_rejects(candidate):
    assert _matches(candidate, "abc123") is False


# ── auth bypass when disabled ─────────────────────────────────────────────
def test_routes_open_when_auth_disabled(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("DASH_AUTH_ENABLED", "false")
    get_settings.cache_clear()
    assert client.get("/api/v1/overview").status_code == 200
