"""
SLAC S3DF Status Adapter for the IRI Facility API.

Serves the IRI ``/status`` router (resources, events, incidents) from
IRI-owned periodic health checks and a local in-memory status cache.

Design (see design-docs/s3df-status-adapter.md):

  * Resources are STATIC config (``app.s3df.status.config``). Each pairs an IRI
    ``Resource`` template with zero or more health checks. Built-in checks are
    merged with optional ``S3DF_STATUS_CHECKS_JSON`` checks at adapter startup.
  * A background poller (``app.s3df.status.poller``) runs every
    ``S3DF_STATUS_POLL_INTERVAL`` seconds, queries each resource's configured
    sources, maps the aggregate result to a Status (up/down/degraded/unknown),
    and records it in an in-memory store (``app.s3df.status.store``).
  * The store detects status TRANSITIONS to emit Events and to open/close
    unplanned Incidents.

This module hosts only the ``FacilityAdapter`` implementation; the registry,
health checker, store and poller live in the ``app.s3df.status`` package.

Lifecycle note: IRI constructs adapters synchronously at import/router-build time
— before the asyncio event loop exists — so the poller and its ``httpx`` client
are created LAZILY on the first request (``_ensure_started``), where the loop is
running. State is in-memory and per-process: it is lost on restart and diverges
across multiple uvicorn workers. Persistence and FastAPI-lifespan shutdown wiring
are tracked as follow-ups in the design doc.
"""

import asyncio
import datetime
import logging

from app.routers.status import facility_adapter as status_adapter
from app.routers.status import models as status_models

from .status.config import MonitoredResource, StatusSettings, build_registry
from .status.poller import StatusPoller
from .status.store import StatusStore

logger = logging.getLogger(__name__)


def _paginate(items: list, offset: int | None, limit: int | None) -> list:
    if offset and offset > 0:
        items = items[offset:]
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


class S3DFStatusAdapter(status_adapter.FacilityAdapter):
    """IRI status FacilityAdapter backed by internal S3DF health checks.

    Implements every method called by the IRI status router:
      get_resources, get_resource, get_events, get_event,
      get_incidents, get_incident.

    The status router is unauthenticated, so these methods take no user.
    """

    def __init__(
        self,
        settings: StatusSettings | None = None,
        monitored: list[MonitoredResource] | None = None,
    ):
        self._settings = settings or StatusSettings()
        self._monitored = monitored if monitored is not None else build_registry(self._settings)
        self._store = StatusStore(
            site_id=self._settings.site_id,
            resources=[m.resource for m in self._monitored],
        )
        self._poller = StatusPoller(self._store, self._settings, self._monitored)
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
