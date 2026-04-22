"""Tests for app.railway_client — GraphQL wrapper for Railway billing API."""

from __future__ import annotations

import json

import httpx
import pytest

from app import railway_client


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj-abc")
    yield


@pytest.fixture
def _unconfigured(monkeypatch):
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    yield


def _make_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, timeout=5)


class TestIsConfigured:
    def test_false_when_missing(self, _unconfigured):
        assert railway_client.is_configured() is False

    def test_true_when_both_set(self, _configured):
        assert railway_client.is_configured() is True

    def test_false_with_only_token(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "x")
        monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
        assert railway_client.is_configured() is False

    def test_false_with_only_project(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
        assert railway_client.is_configured() is False


class TestFetchUsage:
    def test_returns_not_available_without_token(self, _unconfigured):
        result = railway_client.fetch_usage()
        assert result == {"available": False, "reason": "no_token"}

    def test_sends_auth_and_parses_response(self, _configured):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={
                "data": {
                    "project": {
                        "id": "proj-abc",
                        "name": "Vs News",
                        "services": {"edges": [
                            {"node": {"id": "svc-1", "name": "web"}},
                            {"node": {"id": "svc-2", "name": "ollama"}},
                        ]},
                    },
                    # 1 vCPU-month ≈ 43_200 vCPU-minutes → $20.00
                    # 2 GB-month ≈ 86_400 GB-minutes → $20.00
                    # 10 GB egress → $0.50
                    # 5 GB-month ≈ 216_000 GB-minutes → $1.25
                    "estimatedUsage": [
                        {"measurement": "CPU_USAGE", "estimatedValue": 43_200.0},
                        {"measurement": "MEMORY_USAGE_GB", "estimatedValue": 86_400.0},
                        {"measurement": "NETWORK_TX_GB", "estimatedValue": 10.0},
                        {"measurement": "DISK_USAGE_GB", "estimatedValue": 216_000.0},
                    ],
                }
            })

        with _make_client(handler) as client:
            result = railway_client.fetch_usage(client=client)

        assert captured["auth"] == "Bearer test-token"
        variables = captured["body"]["variables"]
        assert variables["projectId"] == "proj-abc"
        assert "CPU_USAGE" in variables["measurements"]
        assert "MEMORY_USAGE_GB" in variables["measurements"]

        assert result["available"] is True
        names = [s["service_name"] for s in result["services"]]
        assert "web" in names
        assert "ollama" in names
        # 20 + 20 + 0.5 + 1.25 = 41.75
        assert result["total_usd_month"] == pytest.approx(41.75, rel=1e-3)

    def test_returns_unavailable_on_http_error(self, _configured):
        def handler(request):
            return httpx.Response(401, json={"error": "unauthorized"})

        with _make_client(handler) as client:
            result = railway_client.fetch_usage(client=client)
        assert result["available"] is False
        assert result["reason"] == "http_401"

    def test_returns_unavailable_on_graphql_errors(self, _configured):
        def handler(request):
            return httpx.Response(200, json={"errors": [{"message": "bad"}]})

        with _make_client(handler) as client:
            result = railway_client.fetch_usage(client=client)
        assert result["available"] is False
        assert result["reason"] == "error"

    def test_returns_unavailable_on_timeout(self, _configured):
        def handler(request):
            raise httpx.TimeoutException("slow", request=request)

        with _make_client(handler) as client:
            result = railway_client.fetch_usage(client=client)
        assert result["available"] is False
        assert result["reason"] == "timeout"

    def test_missing_estimated_usage_defaults_to_zero(self, _configured):
        def handler(request):
            return httpx.Response(200, json={
                "data": {
                    "project": {
                        "id": "p", "name": "P",
                        "services": {"edges": []},
                    },
                    "estimatedUsage": None,
                }
            })

        with _make_client(handler) as client:
            result = railway_client.fetch_usage(client=client)
        assert result["available"] is True
        assert result["total_usd_month"] == 0.0

    def test_empty_project_data_is_handled(self, _configured):
        def handler(request):
            return httpx.Response(200, json={"data": {"project": None, "estimatedUsage": []}})

        with _make_client(handler) as client:
            result = railway_client.fetch_usage(client=client)
        assert result["available"] is True
        assert result["total_usd_month"] == 0.0
        assert isinstance(result["services"], list)

    def test_unknown_measurement_contributes_zero(self, _configured):
        def handler(request):
            return httpx.Response(200, json={
                "data": {
                    "project": {"id": "p", "name": "P", "services": {"edges": []}},
                    "estimatedUsage": [
                        {"measurement": "SOMETHING_NEW", "estimatedValue": 999.0},
                        {"measurement": "NETWORK_TX_GB", "estimatedValue": 4.0},
                    ],
                }
            })

        with _make_client(handler) as client:
            result = railway_client.fetch_usage(client=client)
        assert result["available"] is True
        # Only NETWORK_TX_GB contributes: 4 GB * $0.05 = $0.20
        assert result["total_usd_month"] == pytest.approx(0.20, rel=1e-3)
