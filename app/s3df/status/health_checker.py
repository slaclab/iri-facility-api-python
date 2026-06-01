"""
Health checker for the S3DF status adapter.

Runs a :class:`~app.s3df.status.config.HealthCheck` against its metrics backend
(Prometheus or InfluxDB) over ``httpx`` and reduces the response to a single
:class:`~app.routers.status.models.Status` via :func:`evaluate`.
"""

import datetime
import logging
from dataclasses import dataclass

import httpx

from app.routers.status.models import Status

from .config import Backend, HealthCheck, StatusSettings, utc_now

logger = logging.getLogger(__name__)


@dataclass
class HealthResult:
    """Outcome of a single health check."""

    status: Status
    value: float | None
    observed_at: datetime.datetime
    error: str | None = None


def evaluate(check: HealthCheck, value: float | None) -> Status:
    """Map an observed metric value to a Status using the check's conditions."""
    if value is None:
        return Status.unknown
    if check.up_when.met(value):
        return Status.up
    if check.degraded_when is not None and check.degraded_when.met(value):
        return Status.degraded
    return Status.down


class HealthChecker:
    """Runs a HealthCheck against its backend and evaluates the result.

    The caller owns the ``httpx.AsyncClient`` lifecycle (created from within the
    running event loop) and passes it in.
    """

    def __init__(self, settings: StatusSettings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def check(self, check: HealthCheck) -> HealthResult:
        now = utc_now()
        try:
            if check.backend == Backend.prometheus:
                value = await self._prometheus_query(check.query)
            elif check.backend == Backend.influxdb:
                db = check.db_name or self.settings.influxdb_db
                value = await self._influx_query(db, check.query)
            else:  # pragma: no cover - guarded by enum
                return HealthResult(Status.unknown, None, now, f"unknown backend {check.backend}")
        except Exception as exc:  # noqa: BLE001 - any failure -> unknown
            logger.warning("Health check failed (%s): %s", check.backend.value, exc)
            return HealthResult(Status.unknown, None, now, str(exc))
        return HealthResult(evaluate(check, value), value, now)

    async def _prometheus_query(self, query: str) -> float | None:
        """Instant query via the Prometheus HTTP API; returns the scalar value."""
        url = self.settings.prometheus_url.rstrip("/") + "/api/v1/query"
        resp = await self.client.get(url, params={"query": query}, timeout=self.settings.http_timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"prometheus error: {data.get('error', 'unknown')}")
        result = data.get("data", {}).get("result", [])
        if not result:
            return None
        # Instant vector sample: value == [epoch_ts, "stringified_value"]
        return float(result[0]["value"][1])

    async def _influx_query(self, db_name: str, query: str) -> float | None:
        """Query the InfluxDB HTTP API; returns the most recent scalar value."""
        url = self.settings.influxdb_url.rstrip("/") + "/query"
        resp = await self.client.get(
            url, params={"q": query, "db": db_name}, timeout=self.settings.http_timeout
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        series = results[0].get("series")
        if not series:
            return None
        values = series[0].get("values")
        if not values:
            return None
        # Each row is [time, value]; take the latest row's value column.
        row = values[-1]
        return float(row[1]) if row[1] is not None else None
