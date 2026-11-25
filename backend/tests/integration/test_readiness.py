from fastapi.testclient import TestClient

from relay.api.app import create_app
from relay.config import Settings


def test_readyz_is_ready_with_live_postgres_and_redis(live_settings: Settings) -> None:
    with TestClient(create_app(live_settings)) as client:
        response = client.get("/api/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": "ok", "redis": "ok"}}
