"""Integration tests for the system health, readiness, and version endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_returns_metadata(client: TestClient) -> None:
    response = client.get("/version")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"name", "version", "environment"}
    assert body["environment"] == "testing"
    assert body["version"]


def test_ready_reports_all_dependencies(client: TestClient) -> None:
    response = client.get("/ready")
    # 200 when every dependency is reachable, 503 otherwise — both are valid here.
    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body["dependencies"]) == {"postgres", "redis", "minio", "qdrant"}
    assert body["status"] in ("ok", "degraded")


def test_openapi_docs_available(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_unknown_route_returns_consistent_error(client: TestClient) -> None:
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert set(body) >= {"error", "message", "details"}
    assert body["error"] == "http_error"
