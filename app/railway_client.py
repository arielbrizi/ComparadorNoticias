"""
Cliente HTTP para la Railway GraphQL API.

Consulta el consumo estimado (costo) de los servicios de un proyecto usando
un account/workspace token. La API es la misma que usa el dashboard de Railway
(`https://backboard.railway.com/graphql/v2`).

El cliente está diseñado para fallar con gracia: si falta el token, la red
falla o la query no devuelve datos, devolvemos una lista vacía y loggeamos,
sin romper al caller (el scheduler / endpoint admin).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RAILWAY_API_URL = "https://backboard.railway.com/graphql/v2"
DEFAULT_TIMEOUT = 15.0


def _get_token() -> str | None:
    token = os.environ.get("RAILWAY_API_TOKEN")
    return token.strip() if token else None


def _get_project_id() -> str | None:
    pid = os.environ.get("RAILWAY_PROJECT_ID")
    return pid.strip() if pid else None


def is_configured() -> bool:
    """True if both the API token and project ID env vars are set."""
    return bool(_get_token()) and bool(_get_project_id())


# GraphQL document: combines the project lookup (id/name/services) with
# the top-level `estimatedUsage` query, which is what the Railway dashboard
# actually uses under the hood. `estimatedUsage` is NOT a field on Project —
# it's a root query that returns a list of `{ measurement, estimatedValue }`
# per requested MetricMeasurement. We ask for the major cost drivers and
# convert the raw values to USD in `_normalize_services` using Railway's
# published unit prices (see `_USD_PER_UNIT`).
_USAGE_QUERY = """
query ProjectUsage($projectId: String!, $measurements: [MetricMeasurement!]!) {
  project(id: $projectId) {
    id
    name
    services {
      edges {
        node {
          id
          name
        }
      }
    }
  }
  estimatedUsage(projectId: $projectId, measurements: $measurements) {
    measurement
    estimatedValue
  }
}
""".strip()

# Measurements we ask Railway to estimate for the current billing period.
# Kept narrow on purpose: these are the only ones that materially drive
# monthly cost for a typical Railway project.
_MEASUREMENTS: list[str] = [
    "CPU_USAGE",
    "MEMORY_USAGE_GB",
    "NETWORK_TX_GB",
    "DISK_USAGE_GB",
]

# USD conversion rates per MetricMeasurement, aligned with Railway's published
# 2026 pricing. Units are whatever Railway returns in `estimatedValue`:
#   - CPU_USAGE is reported in vCPU-minutes
#   - MEMORY_USAGE_GB is reported in GB-minutes
#   - NETWORK_TX_GB is reported in GB (cumulative egress)
#   - DISK_USAGE_GB is reported in GB-minutes
# See https://docs.railway.com/pricing/plans for the authoritative rates.
# A month is approximated as 30 days * 24 h * 60 min = 43_200 minutes.
_MINUTES_PER_MONTH = 30 * 24 * 60
_USD_PER_UNIT: dict[str, float] = {
    "CPU_USAGE": 20.0 / _MINUTES_PER_MONTH,        # $20 / vCPU-month
    "MEMORY_USAGE_GB": 10.0 / _MINUTES_PER_MONTH,  # $10 / GB-month
    "NETWORK_TX_GB": 0.05,                          # $0.05 / GB egress
    "DISK_USAGE_GB": 0.25 / _MINUTES_PER_MONTH,    # $0.25 / GB-month
}


def _post(
    query: str,
    variables: dict,
    *,
    token: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Send a GraphQL request. Returns the parsed `data` dict or raises."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"query": query, "variables": variables}

    close_after = False
    if client is None:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT)
        close_after = True
    try:
        resp = client.post(RAILWAY_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
    finally:
        if close_after:
            client.close()

    if body.get("errors"):
        raise RuntimeError(f"Railway API errors: {body['errors']}")
    return body.get("data") or {}


def _estimate_total_usd(estimated_usage: list[dict]) -> tuple[float, list[dict]]:
    """Sum estimated usage entries to a monthly USD total.

    Returns `(total_usd, breakdown)` where `breakdown` is a list of
    `{measurement, estimated_value, usd_month}` rows suitable for the `raw`
    payload. Unknown measurements contribute $0 and are kept for debugging.
    """
    total = 0.0
    breakdown: list[dict] = []
    for entry in estimated_usage or []:
        if not isinstance(entry, dict):
            continue
        measurement = entry.get("measurement") or ""
        try:
            raw_value = float(entry.get("estimatedValue") or 0.0)
        except (TypeError, ValueError):
            raw_value = 0.0
        rate = _USD_PER_UNIT.get(measurement, 0.0)
        usd = raw_value * rate
        total += usd
        breakdown.append({
            "measurement": measurement,
            "estimated_value": raw_value,
            "usd_month": round(usd, 4),
        })
    return total, breakdown


def _normalize_services(data: dict) -> list[dict]:
    """Convert the GraphQL response into `[{service_name, service_id, usd_month, raw}]`.

    Note: Railway's public API doesn't split estimated cost per-service; we
    attribute the total project estimate to a single synthetic "project total"
    row and list each service with `usd_month=None` for transparency.
    """
    project = data.get("project") or {}
    total, breakdown = _estimate_total_usd(data.get("estimatedUsage") or [])

    services = (project.get("services") or {}).get("edges") or []
    rows: list[dict] = []

    for edge in services:
        node = (edge or {}).get("node") or {}
        rows.append({
            "service_name": node.get("name") or node.get("id") or "—",
            "service_id": node.get("id") or "",
            "usd_month": None,
            "raw": dict(node),
        })

    rows.append({
        "service_name": project.get("name") or "Proyecto",
        "service_id": project.get("id") or "",
        "usd_month": round(total, 4),
        "raw": {"_aggregate": True, "breakdown": breakdown},
    })

    return rows


# ── Service logs ─────────────────────────────────────────────────────────────
#
# Railway exposes a ``deploymentLogs`` GraphQL query that returns the log
# stream of a specific deployment. To use it we first need:
#   1) the service ID for the service called (by convention) "ollama"
#   2) the ID of its latest deployment
# and then we call ``deploymentLogs(deploymentId, limit, filter)``.
#
# The exact field names below match Railway's public GraphQL schema as of
# 2025/2026; if the schema changes we fail gracefully with a clear reason.

_SERVICES_QUERY = """
query ProjectServices($projectId: String!) {
  project(id: $projectId) {
    services {
      edges { node { id name } }
    }
  }
}
""".strip()

_LATEST_DEPLOY_QUERY = """
query LatestDeployment($projectId: String!, $serviceId: String!) {
  deployments(
    first: 1,
    input: { projectId: $projectId, serviceId: $serviceId }
  ) {
    edges { node { id status createdAt } }
  }
}
""".strip()

_DEPLOYMENT_LOGS_QUERY = """
query DeploymentLogs($deploymentId: String!, $limit: Int, $filter: String) {
  deploymentLogs(deploymentId: $deploymentId, limit: $limit, filter: $filter) {
    message
    timestamp
    severity
  }
}
""".strip()


def _find_service_id(
    service_name: str,
    *,
    project_id: str,
    token: str,
    client: httpx.Client | None = None,
) -> tuple[str | None, list[str]]:
    """Return (service_id, known_names) for the service whose name matches.

    The match is case-insensitive and trims whitespace. ``known_names`` is
    the full list of services in the project, used for "service not found"
    error messages.
    """
    data = _post(_SERVICES_QUERY, {"projectId": project_id},
                 token=token, client=client)
    project = data.get("project") or {}
    edges = (project.get("services") or {}).get("edges") or []
    target = (service_name or "").strip().lower()
    known: list[str] = []
    match_id: str | None = None
    for edge in edges:
        node = (edge or {}).get("node") or {}
        name = node.get("name") or ""
        known.append(name)
        if name.strip().lower() == target:
            match_id = node.get("id")
    return match_id, known


def _latest_deployment_id(
    service_id: str,
    *,
    project_id: str,
    token: str,
    client: httpx.Client | None = None,
) -> str | None:
    """Return the ID of the most recent deployment for a service, or None."""
    data = _post(
        _LATEST_DEPLOY_QUERY,
        {"projectId": project_id, "serviceId": service_id},
        token=token,
        client=client,
    )
    edges = (data.get("deployments") or {}).get("edges") or []
    if not edges:
        return None
    node = (edges[0] or {}).get("node") or {}
    return node.get("id") or None


def fetch_service_logs(
    service_name: str | None = None,
    *,
    limit: int = 200,
    filter: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch recent log lines for a Railway service (default: "ollama").

    Returns a dict shaped like:
        {
          "available": bool,
          "reason": str,            # only if available=False
          "service_name": str,
          "service_id": str,
          "deployment_id": str,
          "logs": [                 # most recent first as returned by Railway
            {"timestamp": ..., "severity": ..., "message": ...},
            ...
          ],
        }
    """
    token = _get_token()
    project_id = _get_project_id()
    if not token or not project_id:
        return {"available": False, "reason": "no_token"}

    target_name = (
        service_name
        or os.environ.get("RAILWAY_OLLAMA_SERVICE_NAME", "ollama")
    ).strip() or "ollama"

    # Clamp limit to a sane range. Railway accepts a wide range but we don't
    # want to flood the admin panel either.
    limit = max(1, min(int(limit or 200), 1000))

    close_after = False
    if client is None:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT)
        close_after = True

    try:
        try:
            service_id, known = _find_service_id(
                target_name, project_id=project_id, token=token, client=client,
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("Railway services query HTTP error: %s", exc)
            return {
                "available": False,
                "reason": f"http_{exc.response.status_code}",
                "service_name": target_name,
            }
        except httpx.TimeoutException:
            return {"available": False, "reason": "timeout", "service_name": target_name}
        except Exception as exc:
            logger.warning("Railway services query failed: %s", exc)
            return {"available": False, "reason": "error", "service_name": target_name}

        if not service_id:
            return {
                "available": False,
                "reason": "service_not_found",
                "service_name": target_name,
                "known_services": known,
            }

        try:
            deployment_id = _latest_deployment_id(
                service_id, project_id=project_id, token=token, client=client,
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("Railway deployments query HTTP error: %s", exc)
            return {
                "available": False,
                "reason": f"http_{exc.response.status_code}",
                "service_name": target_name,
                "service_id": service_id,
            }
        except Exception as exc:
            logger.warning("Railway deployments query failed: %s", exc)
            return {
                "available": False, "reason": "error",
                "service_name": target_name, "service_id": service_id,
            }

        if not deployment_id:
            return {
                "available": False,
                "reason": "no_deployment",
                "service_name": target_name,
                "service_id": service_id,
            }

        try:
            data = _post(
                _DEPLOYMENT_LOGS_QUERY,
                {"deploymentId": deployment_id, "limit": limit, "filter": filter or ""},
                token=token,
                client=client,
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("Railway deploymentLogs HTTP error: %s", exc)
            return {
                "available": False,
                "reason": f"http_{exc.response.status_code}",
                "service_name": target_name,
                "service_id": service_id,
                "deployment_id": deployment_id,
            }
        except Exception as exc:
            logger.warning("Railway deploymentLogs failed: %s", exc)
            return {
                "available": False, "reason": "error",
                "service_name": target_name, "service_id": service_id,
                "deployment_id": deployment_id,
            }

        raw_logs = data.get("deploymentLogs") or []
        logs = [
            {
                "timestamp": (entry or {}).get("timestamp"),
                "severity": (entry or {}).get("severity"),
                "message": (entry or {}).get("message") or "",
            }
            for entry in raw_logs
        ]
        return {
            "available": True,
            "service_name": target_name,
            "service_id": service_id,
            "deployment_id": deployment_id,
            "logs": logs,
        }
    finally:
        if close_after:
            client.close()


def fetch_usage(client: httpx.Client | None = None) -> dict[str, Any]:
    """Fetch the current usage snapshot.

    Returns a dict:
        {
          "available": bool,
          "reason": str (only if available=False),
          "services": [...],  # only if available=True
          "total_usd_month": float,
        }
    """
    token = _get_token()
    project_id = _get_project_id()

    if not token or not project_id:
        return {"available": False, "reason": "no_token"}

    try:
        data = _post(
            _USAGE_QUERY,
            {"projectId": project_id, "measurements": list(_MEASUREMENTS)},
            token=token,
            client=client,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning("Railway API HTTP error: %s", exc)
        return {"available": False, "reason": f"http_{exc.response.status_code}"}
    except httpx.TimeoutException:
        logger.warning("Railway API timed out")
        return {"available": False, "reason": "timeout"}
    except Exception as exc:
        logger.warning("Railway API failed: %s", exc)
        return {"available": False, "reason": "error"}

    services = _normalize_services(data)
    total = sum(
        (s.get("usd_month") or 0.0)
        for s in services
        if s.get("raw", {}).get("_aggregate")
    )
    return {
        "available": True,
        "services": services,
        "total_usd_month": round(total, 4),
    }
