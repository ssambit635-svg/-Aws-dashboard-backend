"""The deploy manifests are executable configuration, so test them like code.

A YAML typo, a stale port, or a health-check path that no longer exists fails
silently: the deploy succeeds, the service starts, and the platform quietly
marks it unhealthy or routes nothing to it. Worse, a manifest that sets
``DASH_ENVIRONMENT=production`` without supplying a key would hit the startup
guard *in production only* — every local test would still pass.

So each test here applies a manifest as-is and asserts the application would
come up. If a manifest cannot construct ``Settings``, the deploy would fail.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from app.core.config import DEFAULT_API_KEY, Settings
from app.main import app

ROOT = Path(__file__).resolve().parent.parent


# ── helpers ───────────────────────────────────────────────────────────────
def app_routes() -> set[str]:
    return set(app.openapi()["paths"])


def settings_kwargs(environ: dict[str, str]) -> dict[str, object]:
    """Turn DASH_* environment variables into Settings constructor arguments."""
    return {
        name.removeprefix("DASH_").lower(): value
        for name, value in environ.items()
        if name.startswith("DASH_")
    }


def render_service() -> dict:
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    return blueprint["services"][0]


def render_environment() -> dict[str, str]:
    return {item["key"]: item["value"] for item in render_service().get("envVars", [])}


def fly_config() -> dict:
    return tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))


def task_definition() -> dict:
    return json.loads((ROOT / "deploy/aws/task-definition.json").read_text(encoding="utf-8"))


def task_environment() -> dict[str, str]:
    container = task_definition()["containerDefinitions"][0]
    environ = {item["name"]: item["value"] for item in container.get("environment", [])}
    # Secrets are injected by the platform at runtime; supply a real-looking one
    # so the simulation exercises the private shape, not the demo fallback.
    for secret in container.get("secrets", []):
        environ[secret["name"]] = "t" * 43
    return environ


def dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


# ── Render ────────────────────────────────────────────────────────────────
def test_render_blueprint_is_a_docker_web_service():
    service = render_service()
    assert service["type"] == "web"
    assert service["runtime"] == "docker"
    # A free plan is what makes a portfolio link free; anything else silently
    # starts billing the person who clicks the button.
    assert service["plan"] == "free"


def test_render_points_at_a_dockerfile_that_exists():
    path = (ROOT / render_service()["dockerfilePath"]).resolve()
    assert path.is_file(), f"{path} does not exist"


def test_render_health_check_path_is_a_real_route():
    path = render_service()["healthCheckPath"]
    assert path in app_routes(), f"{path} is not served by the app: {sorted(app_routes())}"
    # The probe must be cheap and dependency-free, not a data endpoint that
    # talks to AWS: a slow probe gets the service killed under load.
    assert path in {"/livez", "/readyz"}


def test_render_environment_boots_the_app():
    settings = Settings(**settings_kwargs(render_environment()))
    assert settings.environment == "production"


def test_render_does_not_embed_a_secret():
    for name in render_environment():
        if "KEY" in name or "SECRET" in name:
            pytest.fail(f"{name} is committed to render.yaml; use sync: false")


# ── Fly ───────────────────────────────────────────────────────────────────
def test_fly_environment_boots_the_app():
    settings = Settings(**settings_kwargs(fly_config()["env"]))
    assert settings.environment == "production"


def test_fly_internal_port_matches_the_image_default():
    service = fly_config()["http_service"]
    # The container binds $PORT, and fly.toml pins PORT to internal_port. If
    # these diverge, Fly health-checks a closed port and the app never deploys.
    assert int(fly_config()["env"]["PORT"]) == service["internal_port"]
    # And with no PORT override at all, the image must still bind 8000 — that is
    # the number every manifest and the EXPOSE line agree on.
    assert "${PORT:-8000}" in dockerfile()


def test_fly_health_check_path_is_a_real_route():
    checks = fly_config()["http_service"]["checks"]
    assert checks, "no health check configured"
    for check in checks:
        assert check["path"] in app_routes()


# ── ECS ───────────────────────────────────────────────────────────────────
def test_task_definition_boots_the_app():
    settings = Settings(**settings_kwargs(task_environment()))
    assert settings.environment == "production"
    # The private shape: auth on, and a key that is not the committed default.
    assert settings.auth_enabled is True
    assert settings.api_key != DEFAULT_API_KEY


def test_task_definition_never_inlines_aws_credentials():
    """Static keys in a task definition end up in the API and in git history."""
    environ = task_environment()
    leaked = {name for name in environ if name.startswith("AWS_") and name != "AWS_REGION"}
    assert not leaked, f"credentials must come from the task role, found {sorted(leaked)}"
    assert task_definition().get("taskRoleArn"), "no task role: the SDK has nothing to use"


def test_task_definition_container_port_matches_the_image():
    container = task_definition()["containerDefinitions"][0]
    ports = [mapping["containerPort"] for mapping in container["portMappings"]]
    assert ports == [8000]
    assert "EXPOSE 8000" in dockerfile()


def test_task_definition_health_check_uses_a_real_route():
    container = task_definition()["containerDefinitions"][0]
    command = " ".join(container["healthCheck"]["command"])
    assert "/livez" in command
    assert "8000" in command


# ── the image itself ──────────────────────────────────────────────────────
def test_image_binds_all_interfaces():
    """0.0.0.0, not 127.0.0.1: a container bound to loopback is unreachable."""
    assert "--host 0.0.0.0" in dockerfile()


def test_image_takes_its_port_from_the_environment():
    """Render, Fly, Cloud Run and ECS all assign the port through $PORT."""
    command = re.search(r"^CMD \[(.*)\]$", dockerfile(), re.MULTILINE)
    assert command, "no CMD found"
    assert "${PORT:-8000}" in command.group(1)


def test_image_health_check_follows_the_assigned_port():
    """A healthcheck pinned to 8000 marks the container unhealthy on Render."""
    probe = re.search(r"^HEALTHCHECK.*?\n(.*?)$", dockerfile(), re.MULTILINE | re.DOTALL)
    assert probe, "no HEALTHCHECK found"
    line = probe.group(1)
    assert "os.environ.get('PORT'" in line
    assert "127.0.0.1:8000" not in line


def test_image_runs_as_non_root():
    assert re.search(r"^USER (?!root)", dockerfile(), re.MULTILINE)
