from fastapi.testclient import TestClient

from relay.api.app import create_app
from relay.config import Settings


def test_healthz_reports_ok_without_any_dependency(offline_settings: Settings) -> None:
    with TestClient(create_app(offline_settings)) as client:
        response = client.get("/api/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_503_and_names_each_failed_dependency(offline_settings: Settings) -> None:
    with TestClient(create_app(offline_settings)) as client:
        response = client.get("/api/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"postgres": "error", "redis": "error"},
    }


def test_openapi_schema_is_served_under_the_api_prefix(offline_settings: Settings) -> None:
    with TestClient(create_app(offline_settings)) as client:
        response = client.get("/api/openapi.json")

    assert response.status_code == 200
    assert "/api/readyz" in response.json()["paths"]
