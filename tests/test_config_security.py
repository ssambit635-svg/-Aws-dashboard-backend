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


def test_api_key_injection_is_refused_in_staging():
    """Staging is a shared environment: the key must not reach page source."""
    staging = Settings(environment="staging", bootstrap_dashboard=True)
    assert staging.api_key_injected is False


def test_api_key_injection_can_be_disabled():
    settings = Settings(environment="local", bootstrap_dashboard=False)
    assert settings.api_key_injected is False


def test_auth_can_be_disabled():
    settings = Settings(auth_enabled=False, api_key="anything")
    assert settings.effective_api_key is None


# ── .env.example stays honest ─────────────────────────────────────────────
def test_env_example_documents_only_real_settings():
    """A stale key in .env.example silently does nothing when a user sets it."""
    import re
    from pathlib import Path

    example = Path(".env.example").read_text(encoding="utf-8")
    documented = {
        match.group(1).lower().replace("dash_", "")
        for match in re.finditer(r"^#?\s*(DASH_[A-Z_]+)=", example, re.MULTILINE)
    }
    assert documented, "no DASH_ variables parsed from .env.example"
    unknown = documented - set(Settings.model_fields)
    assert not unknown, f".env.example documents settings that do not exist: {sorted(unknown)}"


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


# ── the production key guard ──────────────────────────────────────────────
def test_production_refuses_the_shipped_development_key():
    """The repo is public, so the default key authenticates everybody."""
    from app.core.config import DEFAULT_API_KEY

    with pytest.raises(ValidationError) as excinfo:
        Settings(environment="production", api_key=DEFAULT_API_KEY)
    message = str(excinfo.value)
    assert "DASH_API_KEY" in message
    # The error has to say how to fix it, not only that it is wrong.
    assert "token_urlsafe" in message
    assert "DASH_AUTH_ENABLED=false" in message


def test_production_accepts_a_generated_key():
    settings = Settings(environment="production", api_key="q" * 43)
    assert settings.environment == "production"


def test_production_allows_a_keyless_public_demo():
    """Auth off means no secret to leak, so the public demo is legitimate."""
    settings = Settings(environment="production", auth_enabled=False)
    assert settings.effective_api_key is None


def test_local_development_keeps_working_with_the_default_key():
    from app.core.config import DEFAULT_API_KEY

    # api_key passed explicitly: the test environment sets its own DASH_API_KEY,
    # and env vars would otherwise win over the value under test.
    assert Settings(environment="local", api_key=DEFAULT_API_KEY).api_key == DEFAULT_API_KEY


def test_staging_is_not_held_to_the_production_guard():
    """The guard is about public exposure; staging still needs a real key by
    convention, but a developer running staging locally is not bricked."""
    assert Settings(environment="staging").environment == "staging"


def test_container_image_defaults_to_production():
    """If the Dockerfile defaulted to local, the guard would never fire and a
    deployed container would inject the development key into page source."""
    from pathlib import Path

    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "DASH_ENVIRONMENT=production" in dockerfile
    assert "DASH_ENVIRONMENT=local" not in dockerfile


# ── deployment manifests stay in sync with Settings ───────────────────────
def _deploy_env_names() -> set[str]:
    import json
    import re
    from pathlib import Path

    names: set[str] = set()

    render = Path("render.yaml").read_text(encoding="utf-8")
    names |= {m.group(1) for m in re.finditer(r"-\s*key:\s*([A-Z_]+)", render)}

    fly = Path("fly.toml").read_text(encoding="utf-8")
    names |= {m.group(1) for m in re.finditer(r"^\s*([A-Z][A-Z0-9_]+)\s*=", fly, re.MULTILINE)}

    task = json.loads(Path("deploy/aws/task-definition.json").read_text(encoding="utf-8"))
    for container in task["containerDefinitions"]:
        names |= {item["name"] for item in container.get("environment", [])}
        names |= {item["name"] for item in container.get("secrets", [])}

    return {name for name in names if name.startswith("DASH_")}


def _known_settings() -> set[str]:
    return {f"dash_{name}".upper() for name in Settings.model_fields}


def test_deploy_manifests_only_set_real_settings():
    """A typo in a manifest is silent: the app boots and ignores the variable."""
    unknown = _deploy_env_names() - _known_settings()
    assert not unknown, f"deploy manifests set settings that do not exist: {sorted(unknown)}"


def test_deploy_manifests_cover_the_settings_that_matter_in_production():
    configured = _deploy_env_names()
    for required in {
        "DASH_ENVIRONMENT",
        "DASH_AUTH_ENABLED",
        "DASH_PROVIDER_MODE",
    }:
        assert required in configured, f"{required} is not set by any deploy manifest"
