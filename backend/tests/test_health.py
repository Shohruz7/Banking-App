from rest_framework.test import APIClient


def test_health_returns_ok(api_client: APIClient) -> None:
    response = api_client.get("/api/v1/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
