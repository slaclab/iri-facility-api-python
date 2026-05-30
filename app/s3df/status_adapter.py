"""
SLAC S3DF Status Adapter for the IRI Facility API.

Serves the IRI ``/status`` router (resources, events, incidents) from live
health checks against S3DF monitoring infrastructure — Prometheus and InfluxDB
(telegraf) — the same backends the standalone ``status-pusher`` tool queries.

Design (see design-docs/s3df-status-adapter.md):

  * Resources are STATIC config (``RESOURCE_REGISTRY``). Each carries a health
    check: a backend + query + condition (comparator, threshold) — mirroring the
    success-criterion model used by ``status-pusher``.
  * A background poller runs every ``S3DF_STATUS_POLL_INTERVAL`` seconds, queries
    each resource's backend, maps the result to a Status (up/down/degraded/
    unknown), and records it in an in-memory store.
  * The store detects status TRANSITIONS to emit Events and to open/close
    unplanned Incidents.

Lifecycle note: IRI constructs adapters synchronously at import/router-build time
— before the asyncio event loop exists — so the poller and its ``httpx`` client
are created LAZILY on the first request (``_ensure_started``), where the loop is
running. State is in-memory and per-process: it is lost on restart and diverges
across multiple uvicorn workers. Persistence and FastAPI-lifespan shutdown wiring
are tracked as follow-ups in the design doc.

Required/optional env vars:
  S3DF_PROMETHEUS_URL       Prometheus base URL   (default https://prometheus.slac.stanford.edu)
  S3DF_INFLUXDB_URL         InfluxDB base URL     (default https://influxdb.slac.stanford.edu)
  S3DF_INFLUXDB_DB          InfluxDB database     (default telegraf)
  S3DF_STATUS_POLL_INTERVAL Seconds between polls (default 60)
  S3DF_STATUS_HTTP_TIMEOUT  Per-query timeout sec (default 15)
  S3DF_SITE_ID              Site id for resources (default s3df)
  S3DF_STATUS_TLS_VERIFY    true | false | <ca-bundle-path>  (default false)
"""

import asyncio
import datetime
import logging
import operator
import os
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import httpx

from app.routers.status import facility_adapter as status_adapter
from app.routers.status import models as status_models
from app.routers.status.models import (
    Event,
    Incident,
    IncidentType,
    Resolution,
    Resource,
    ResourceType,
    Status,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Configuration / resource registry
# ---------------------------------------------------------------------------


class Backend(str, Enum):
    """Metrics backend a health check queries."""

    prometheus = "prometheus"
    influxdb = "influxdb"


_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "lte": operator.le,
    "gt": operator.gt,
    "gte": operator.ge,
}


@dataclass(frozen=True)
class Condition:
    """A comparison of an observed metric value against a threshold."""

    comparator: str
    value: float

    def met(self, observed: float) -> bool:
        op = _COMPARATORS.get(self.comparator)
        if op is None:
            raise ValueError(f"Unknown comparator: {self.comparator}")
        return bool(op(observed, self.value))


@dataclass(frozen=True)
class HealthCheck:
    """How to determine a resource's status from a metrics backend."""

    backend: Backend
    query: str
    up_when: Condition
    db_name: str | None = None  # InfluxDB only
    degraded_when: Condition | None = None


@dataclass(frozen=True)
class ResourceDef:
    """Static definition of a monitored resource + its health check."""

    id: str
    name: str
    description: str
    group: str
    resource_type: ResourceType
    health_check: HealthCheck
    capability_ids: tuple[str, ...] = ()


class StatusSettings:
    """Environment-driven settings for the S3DF status adapter."""

    def __init__(self) -> None:
        self.prometheus_url = os.getenv("S3DF_PROMETHEUS_URL", "https://prometheus.slac.stanford.edu")
        self.influxdb_url = os.getenv("S3DF_INFLUXDB_URL", "https://influxdb.slac.stanford.edu")
        self.influxdb_db = os.getenv("S3DF_INFLUXDB_DB", "telegraf")
        self.poll_interval = int(os.getenv("S3DF_STATUS_POLL_INTERVAL", "60"))
        self.http_timeout = float(os.getenv("S3DF_STATUS_HTTP_TIMEOUT", "15"))
        # NOTE: the /facility adapter currently mints a random site uuid per
        # process, so this id is not a guaranteed cross-reference yet. Set
        # S3DF_SITE_ID once a stable site identifier is established.
        self.site_id = os.getenv("S3DF_SITE_ID", "s3df")
        self.tls_verify = self._parse_verify(os.getenv("S3DF_STATUS_TLS_VERIFY", "false"))

    @staticmethod
    def _parse_verify(raw: str) -> bool | str:
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
        # Anything else is treated as a path to a CA bundle.
        return raw


# The set of S3DF resources surfaced by /status. Queries mirror those exercised
# by status-pusher's live tests (see status-pusher/Makefile). Extend as needed.
RESOURCE_REGISTRY: list[ResourceDef] = [
    ResourceDef(
        id="s3df-ssh-gateway",
        name="SSH Login Gateway",
        description="S3DF interactive SSH login gateway reachability.",
        group="access",
        resource_type=ResourceType.service,
        health_check=HealthCheck(
            backend=Backend.prometheus,
            query="avg( avg_over_time(nmap_port_state{service=`ssh`,group=`s3df`}[5m]) )",
            up_when=Condition("eq", 1.0),
        ),
    ),
    ResourceDef(
        id="s3df-slurmctld",
        name="Slurm Controller (slurmctld)",
        description="Slurm workload manager controller daemon health.",
        group="compute",
        resource_type=ResourceType.compute,
        health_check=HealthCheck(
            backend=Backend.influxdb,
            db_name="telegraf",
            query=(
                'SELECT mean("status_code") FROM "monit_process" '
                "WHERE \"service\" = 'slurmctld' AND time > now()-5m"
            ),
            up_when=Condition("eq", 1.0),
        ),
    ),
    ResourceDef(
        id="s3df-slurmdbd",
        name="Slurm DB Daemon (slurmdbd)",
        description="Slurm accounting database daemon health.",
        group="compute",
        resource_type=ResourceType.compute,
        health_check=HealthCheck(
            backend=Backend.influxdb,
            db_name="telegraf",
            query=(
                'SELECT mean("status_code") FROM "monit_process" '
                "WHERE \"service\" = 'slurmdbd' AND time > now()-5m"
            ),
            up_when=Condition("eq", 1.0),
        ),
    ),
]


# ---------------------------------------------------------------------------
# Health checker — async Prometheus / InfluxDB queries + evaluation
# ---------------------------------------------------------------------------


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
        now = _utc_now()
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


# ---------------------------------------------------------------------------
# In-memory store — current status, events, incidents
# ---------------------------------------------------------------------------


class StatusStore:
    """Holds current status per resource plus an event log and incident set.

    Single-writer model: only the poller calls ``record()``, and ``record()`` has
    no internal ``await`` points, so each mutation is atomic with respect to the
    asyncio event loop. Reader methods return freshly built/copied lists, so a
    request handler never observes a half-applied update. State is per-process and
    volatile.
    """

    def __init__(self, site_id: str, resource_defs: list[ResourceDef]):
        self.site_id = site_id
        self._defs: dict[str, ResourceDef] = {d.id: d for d in resource_defs}
        self._created_at = _utc_now()
        self._status: dict[str, Status] = {}
        self._last_modified: dict[str, datetime.datetime] = {}
        self._events: list[Event] = []
        self._incidents: dict[str, Incident] = {}
        self._open_incident: dict[str, str] = {}  # resource_id -> incident_id

    # -- writer ------------------------------------------------------------

    def record(self, resource_id: str, result: HealthResult) -> None:
        """Record a poll result, emitting an Event + Incident change on transition."""
        if resource_id not in self._defs:
            return
        prev = self._status.get(resource_id)
        new = result.status
        ts = result.observed_at

        if prev == new:
            return  # steady state — nothing to record

        baseline = prev is None
        self._status[resource_id] = new
        self._last_modified[resource_id] = ts

        event = self._make_event(resource_id, new, ts, baseline)

        if new in (Status.down, Status.degraded):
            inc_id = self._open_incident.get(resource_id)
            if inc_id is None:
                incident = self._make_incident(resource_id, new, ts)
                self._incidents[incident.id] = incident
                self._open_incident[resource_id] = incident.id
                inc_id = incident.id
            else:
                incident = self._incidents[inc_id]
                incident.status = new
                incident.last_modified = ts
            event.incident_id = inc_id
            self._incidents[inc_id].event_ids.append(event.id)
        elif new == Status.up:
            inc_id = self._open_incident.pop(resource_id, None)
            if inc_id is not None:
                incident = self._incidents[inc_id]
                incident.status = Status.up
                incident.end = ts
                incident.resolution = Resolution.completed
                incident.last_modified = ts
                event.incident_id = inc_id
                incident.event_ids.append(event.id)
        # Status.unknown: record the event but never open/close incidents —
        # a monitoring-backend failure is not a confirmed resource outage.

        self._events.append(event)

    def _make_event(self, resource_id: str, status: Status, ts: datetime.datetime, baseline: bool) -> Event:
        rdef = self._defs[resource_id]
        verb = "initial status" if baseline else "status changed to"
        return Event(
            id=str(uuid.uuid4()),
            name=f"{rdef.name}: {status.value}",
            description=f"{rdef.name} {verb} {status.value}.",
            last_modified=ts,
            occurred_at=ts,
            status=status,
            resource_id=resource_id,
        )

    def _make_incident(self, resource_id: str, status: Status, ts: datetime.datetime) -> Incident:
        rdef = self._defs[resource_id]
        return Incident(
            id=str(uuid.uuid4()),
            name=f"{rdef.name} {status.value}",
            description=f"Automatically opened: {rdef.name} observed {status.value}.",
            last_modified=ts,
            status=status,
            start=ts,
            type=IncidentType.unplanned,
            resolution=Resolution.unresolved,
            resource_ids=[resource_id],
            event_ids=[],
        )

    # -- readers -----------------------------------------------------------

    def resources(self) -> list[Resource]:
        out: list[Resource] = []
        for d in self._defs.values():
            out.append(
                Resource(
                    id=d.id,
                    name=d.name,
                    description=d.description,
                    last_modified=self._last_modified.get(d.id, self._created_at),
                    site_id=self.site_id,
                    group=d.group,
                    resource_type=d.resource_type,
                    current_status=self._status.get(d.id, Status.unknown),
                    capability_ids=list(d.capability_ids),
                )
            )
        return out

    def events(self) -> list[Event]:
        return sorted(self._events, key=lambda e: (e.occurred_at, e.id))

    def incidents(self) -> list[Incident]:
        return sorted(self._incidents.values(), key=lambda i: (i.start, i.id))


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------


class StatusPoller:
    """Periodically runs health checks and feeds results into the store."""

    def __init__(self, store: StatusStore, settings: StatusSettings, resource_defs: list[ResourceDef]):
        self.store = store
        self.settings = settings
        self.resource_defs = resource_defs
        self._client: httpx.AsyncClient | None = None
        self._checker: HealthChecker | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Create the HTTP client (inside the running loop), run one initial poll,
        then launch the periodic background loop."""
        self._client = httpx.AsyncClient(verify=self.settings.tls_verify)
        self._checker = HealthChecker(self.settings, self._client)
        await self._poll_once()  # bounded by per-query timeouts; never raises
        self._task = asyncio.create_task(self._run(), name="s3df-status-poller")
        logger.info(
            "S3DF status poller started (%d resources, interval=%ss)",
            len(self.resource_defs),
            self.settings.poll_interval,
        )

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.poll_interval)
                await self._poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the server alive if the loop dies
            logger.exception("S3DF status poller loop crashed")

    async def _poll_once(self) -> None:
        assert self._checker is not None
        defs = self.resource_defs
        results = await asyncio.gather(
            *(self._checker.check(d.health_check) for d in defs),
            return_exceptions=True,
        )
        for d, res in zip(defs, results):
            if isinstance(res, Exception):
                logger.warning("Health check raised for %s: %s", d.id, res)
                continue
            self.store.record(d.id, res)

    async def aclose(self) -> None:
        """Cancel the loop and close the HTTP client. For tests/lifespan wiring."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


def _paginate(items: list, offset: int | None, limit: int | None) -> list:
    if offset and offset > 0:
        items = items[offset:]
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


class S3DFStatusAdapter(status_adapter.FacilityAdapter):
    """IRI status FacilityAdapter backed by live S3DF health checks.

    Implements every method called by the IRI status router:
      get_resources, get_resource, get_events, get_event,
      get_incidents, get_incident.

    The status router is unauthenticated, so these methods take no user.
    """

    def __init__(self, settings: StatusSettings | None = None, resource_defs: list[ResourceDef] | None = None):
        self._settings = settings or StatusSettings()
        self._resource_defs = resource_defs if resource_defs is not None else RESOURCE_REGISTRY
        self._store = StatusStore(site_id=self._settings.site_id, resource_defs=self._resource_defs)
        self._poller = StatusPoller(self._store, self._settings, self._resource_defs)
        self._started = False
        self._start_lock = asyncio.Lock()

    async def _ensure_started(self) -> None:
        """Lazily start the poller on first use (double-checked under a lock)."""
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            try:
                await self._poller.start()
                self._started = True
            except Exception:  # noqa: BLE001 - retry on the next request
                logger.exception("Failed to start S3DF status poller; will retry")
                raise

    async def aclose(self) -> None:
        """Release background resources (HTTP client + poller task)."""
        await self._poller.aclose()
        self._started = False

    # -- resources ---------------------------------------------------------

    async def get_resources(
        self,
        offset: int,
        limit: int,
        name: str | None = None,
        description: str | None = None,
        group: str | None = None,
        modified_since: datetime.datetime | None = None,
        resource_type: status_models.ResourceType | None = None,
        current_status: status_models.Status | None = None,
        capability=None,
        site_id: str | None = None,
    ) -> list[status_models.Resource]:
        await self._ensure_started()
        items = status_models.Resource.find(
            self._store.resources(),
            name=name,
            description=description,
            modified_since=modified_since,
            group=group,
            resource_type=resource_type,
            current_status=current_status,
            capability=capability,
            site_id=site_id,
        )
        return _paginate(items, offset, limit)

    async def get_resource(self, id_: str) -> status_models.Resource:
        await self._ensure_started()
        return status_models.Resource.find_by_id(self._store.resources(), id_, allow_name=True)

    # -- events ------------------------------------------------------------

    async def get_events(
        self,
        offset: int,
        limit: int,
        incident_id: str | None = None,
        resource_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        status: status_models.Status | None = None,
        from_: datetime.datetime | None = None,
        to: datetime.datetime | None = None,
        time_: datetime.datetime | None = None,
        modified_since: datetime.datetime | None = None,
    ) -> list[status_models.Event]:
        await self._ensure_started()
        items = status_models.Event.find(
            self._store.events(),
            incident_id=incident_id,
            resource_id=resource_id,
            name=name,
            description=description,
            status=status,
            from_=from_,
            to=to,
            time_=time_,
            modified_since=modified_since,
        )
        return _paginate(items, offset, limit)

    async def get_event(self, id_: str) -> status_models.Event:
        await self._ensure_started()
        return status_models.Event.find_by_id(self._store.events(), id_)

    # -- incidents ---------------------------------------------------------

    async def get_incidents(
        self,
        offset: int,
        limit: int,
        name: str | None = None,
        description: str | None = None,
        status: status_models.Status | None = None,
        type_: status_models.IncidentType | None = None,
        from_: datetime.datetime | None = None,
        to: datetime.datetime | None = None,
        time_: datetime.datetime | None = None,
        modified_since: datetime.datetime | None = None,
        resource_id: str | None = None,
        resolution: status_models.Resolution | None = None,
    ) -> list[status_models.Incident]:
        await self._ensure_started()
        items = status_models.Incident.find(
            self._store.incidents(),
            name=name,
            description=description,
            status=status,
            type_=type_,
            from_=from_,
            to=to,
            time_=time_,
            modified_since=modified_since,
            resource_id=resource_id,
            resolution=resolution,
        )
        return _paginate(items, offset, limit)

    async def get_incident(self, id_: str) -> status_models.Incident:
        await self._ensure_started()
        return status_models.Incident.find_by_id(self._store.incidents(), id_)
