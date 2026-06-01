"""
Health checker for the S3DF status adapter.

Runs status-pusher-like checks from IRI-owned configuration against Prometheus,
InfluxDB, or HTTP endpoints, evaluates conditions, and aggregates check results
into one resource status.
"""

import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import Sequence

import httpx

from app.routers.status.models import Status

from .config import Backend, HealthCheck, StatusSettings, utc_now

logger = logging.getLogger(__name__)


@dataclass
class HealthResult:
    """Outcome of one resource health evaluation."""

    status: Status
    value: float | None
    observed_at: datetime.datetime
    error: str | None = None


def evaluate(check: HealthCheck, value: float | None) -> Status:
    """Map an observed scalar value to a Status using the check's conditions."""
    if value is None:
        return Status.unknown
    if check.up_when.met(value):
        return Status.up
    if check.degraded_when is not None and check.degraded_when.met(value):
        return Status.degraded
    return Status.down


def aggregate_results(results: Sequence[HealthResult]) -> HealthResult:
    """Aggregate multiple check results into one resource status.

    Unknown checks are ignored when at least one check produced a usable signal.
    This avoids reporting an outage just because one redundant probe failed.
    """
    observed_at = max((result.observed_at for result in results), default=utc_now())
    if not results:
        return HealthResult(Status.unknown, None, observed_at, "no health checks configured")

    known = [result for result in results if result.status != Status.unknown]
    if not known:
        errors = "; ".join(result.error for result in results if result.error)
        return HealthResult(Status.unknown, None, observed_at, errors or "all health checks returned unknown")

    if any(result.status == Status.down for result in known):
        status = Status.down
    elif any(result.status == Status.degraded for result in known):
        status = Status.degraded
    else:
        status = Status.up

    return HealthResult(status, None, observed_at)


class HealthChecker:
    """Executes health checks over a shared httpx AsyncClient."""

    def __init__(self, settings: StatusSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def check(self, checks: Sequence[HealthCheck]) -> HealthResult:
        if not checks:
            return HealthResult(Status.unknown, None, utc_now(), "no health checks configured")

        results = await asyncio.gather(*(self.check_one(check) for check in checks), return_exceptions=True)
        normalized: list[HealthResult] = []
        for result in results:
            if isinstance(result, HealthResult):
                normalized.append(result)
            else:
                normalized.append(HealthResult(Status.unknown, None, utc_now(), str(result)))
        return aggregate_results(normalized)

    async def check_one(self, check: HealthCheck) -> HealthResult:
        now = utc_now()
        try:
            if check.backend == Backend.prometheus:
                if check.query is None:
                    raise ValueError("query is required for prometheus health checks")
                value = await self._prometheus_query(check.query)
            elif check.backend == Backend.influxdb:
                if check.query is None:
                    raise ValueError("query is required for influxdb health checks")
                db = check.db_name or self.settings.influxdb_db
                value = await self._influx_query(db, check.query)
            elif check.backend == Backend.http:
                value = await self._http_query(check)
            else:  # pragma: no cover - guarded by enum/config validation
                return HealthResult(Status.unknown, None, now, f"unknown backend {check.backend}")
        except Exception as exc:  # noqa: BLE001 - health polling should surface errors as unknown status
            label = f"{check.backend.value}:{check.name}" if check.name else check.backend.value
            logger.warning("Health check failed (%s): %s", label, exc)
            return HealthResult(Status.unknown, None, now, str(exc))
        return HealthResult(evaluate(check, value), value, now)

    async def _prometheus_query(self, query: str) -> float | None:
        url = self.settings.prometheus_url.rstrip("/") + "/api/v1/query"
        resp = await self.client.get(url, params={"query": query}, timeout=self.settings.http_timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus returned status={data.get('status')}")
        result = data.get("data", {}).get("result", [])
        if not result:
            return None
        return float(result[0]["value"][1])

    async def _influx_query(self, db: str, query: str) -> float | None:
        url = self.settings.influxdb_url.rstrip("/") + "/query"
        resp = await self.client.get(url, params={"db": db, "q": query}, timeout=self.settings.http_timeout)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        series = results[0].get("series", [])
        if not series or not series[0].get("values"):
            return None
        return float(series[0]["values"][-1][1])

    async def _http_query(self, check: HealthCheck) -> float:
        if check.url is None:
            raise ValueError("url is required for http health checks")
        response = await self.client.request(
            check.method,
            check.url,
            headers=check.headers,
            timeout=self.settings.http_timeout,
            follow_redirects=check.follow_redirects,
        )
        return float(response.status_code)
