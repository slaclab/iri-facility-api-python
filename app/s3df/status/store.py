"""
In-memory store for the S3DF status adapter.

Holds the current status per resource plus an event log and incident set, and
turns poll results into IRI ``Event`` / ``Incident`` objects on status
transitions. State is per-process and volatile (lost on restart, diverges
across uvicorn workers).
"""

import datetime
import uuid

from app.routers.status.models import (
    Event,
    Incident,
    IncidentType,
    Resolution,
    Resource,
    Status,
)

from .config import utc_now
from .health_checker import HealthResult


class StatusStore:
    """Holds current status per resource plus an event log and incident set.

    Single-writer model: only the poller calls ``record()``, and ``record()`` has
    no internal ``await`` points, so each mutation is atomic with respect to the
    asyncio event loop. Reader methods return freshly built/copied lists, so a
    request handler never observes a half-applied update. State is per-process and
    volatile.

    The store is given the static ``Resource`` templates and is the authority for
    their dynamic fields: it overlays ``site_id``, ``current_status`` and
    ``last_modified`` when projecting a live view in :meth:`resources`.
    """

    def __init__(self, site_id: str, resources: list[Resource]):
        self.site_id = site_id
        self._templates: dict[str, Resource] = {r.id: r for r in resources}
        self._created_at = utc_now()
        self._status: dict[str, Status] = {}
        self._last_modified: dict[str, datetime.datetime] = {}
        self._events: list[Event] = []
        self._incidents: dict[str, Incident] = {}
        self._open_incident: dict[str, str] = {}  # resource_id -> incident_id

    # -- writer ------------------------------------------------------------

    def record(self, resource_id: str, result: HealthResult) -> None:
        """Record a poll result, emitting an Event + Incident change on transition."""
        if resource_id not in self._templates:
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
        rname = self._templates[resource_id].name
        verb = "initial status" if baseline else "status changed to"
        return Event(
            id=str(uuid.uuid4()),
            name=f"{rname}: {status.value}",
            description=f"{rname} {verb} {status.value}.",
            last_modified=ts,
            occurred_at=ts,
            status=status,
            resource_id=resource_id,
        )

    def _make_incident(self, resource_id: str, status: Status, ts: datetime.datetime) -> Incident:
        rname = self._templates[resource_id].name
        return Incident(
            id=str(uuid.uuid4()),
            name=f"{rname} {status.value}",
            description=f"Automatically opened: {rname} observed {status.value}.",
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
        """Project live Resource views by overlaying the runtime-owned fields
        (site_id, current_status, last_modified) onto the static templates."""
        out: list[Resource] = []
        for id_, tmpl in self._templates.items():
            out.append(
                tmpl.model_copy(
                    update={
                        "site_id": self.site_id,
                        "current_status": self._status.get(id_, Status.unknown),
                        "last_modified": self._last_modified.get(id_, self._created_at),
                    }
                )
            )
        return out

    def events(self) -> list[Event]:
        return sorted(self._events, key=lambda e: (e.occurred_at, e.id))

    def incidents(self) -> list[Incident]:
        return sorted(self._incidents.values(), key=lambda i: (i.start, i.id))
