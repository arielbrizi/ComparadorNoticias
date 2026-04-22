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


# GraphQL query: list services in the project plus the project's estimated
# monthly usage. The estimatedUsage field is what the dashboard uses to show
# the "current estimated" cost on the project usage page. Schema may evolve;
# we handle missing fields defensively on the parsing side.
_USAGE_QUERY = """
query ProjectUsage($projectId: String!) {
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
    estimatedUsage {
      estimatedValue
    }
  }
}
""".strip()


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


def _normalize_services(data: dict) -> list[dict]:
    """Convert the GraphQL response into `[{service_name, service_id, usd_month, raw}]`.

    Note: Railway's public API doesn't split estimated cost per-service; we
    attribute the total project estimate to a single synthetic "project total"
    row and list each service with `usd_month=None` for transparency.
    """
    project = data.get("project") or {}
    total = 0.0
    try:
        est = project.get("estimatedUsage")
        if isinstance(est, dict) and est.get("estimatedValue") is not None:
            total = float(est["estimatedValue"])
        elif isinstance(est, (int, float)):
            total = float(est)
    except (TypeError, ValueError):
        total = 0.0

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
        "raw": {"_aggregate": True},
    })

    return rows


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
            {"projectId": project_id},
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
