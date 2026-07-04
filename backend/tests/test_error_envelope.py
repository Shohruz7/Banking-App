"""Every API error must arrive in the envelope locked by ADR-0006."""

from rest_framework.test import APIClient


def test_validation_error_uses_envelope(api_client: APIClient) -> None:
    # Token endpoint with an empty body → DRF validation error, no DB involved.
    response = api_client.post("/api/v1/auth/token/", data={}, format="json")

    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "Invalid input."
    # Per-field breakdown is preserved under details.
    assert "username" in body["error"]["details"]
    assert "password" in body["error"]["details"]


def test_invalid_token_error_uses_envelope(api_client: APIClient) -> None:
    api_client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
    response = api_client.post("/api/v1/auth/token/refresh/", data={}, format="json")

    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"error"}
    assert {"code", "message", "details"} <= set(body["error"])
