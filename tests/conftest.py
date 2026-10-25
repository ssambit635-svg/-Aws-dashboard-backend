"""Shared pytest fixtures.

The suite never talks to AWS: ``DASH_PROVIDER_MODE=mock`` is forced before the
application is imported, and every fixture resets the settings/container caches
so tests cannot leak configuration or cached data into one another.
"""

from __future__ import annotations

import os

os.environ["DASH_PROVIDER_MODE"] = "mock"
os.environ["DASH_API_KEY"] = "test-key-123"
os.environ["DASH_ENVIRONMENT"] = "local"

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.providers.mock import MockProvider
from app.services.container import get_container, reset_container

API_KEY = "test-key-123"


@pytest.fixture(autouse=True)
def _reset_caches():
    """Every test starts with cold settings, a fresh container and empty cache."""
    get_settings.cache_clear()
    reset_container()
    yield
    get_settings.cache_clear()
    reset_container()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


@pytest.fixture
def authed(client: TestClient, auth: dict[str, str]) -> TestClient:
    client.headers.update(auth)
    return client


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def container():
    return get_container()
