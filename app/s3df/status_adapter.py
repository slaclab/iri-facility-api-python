"""
SLAC S3DF Status Adapter for the IRI Facility API.

Serves the IRI ``/status`` router (resources, events, incidents) by
forwarding queries to ``s3df-status-api`` and merging dynamic status
with the static resource metadata kept in :mod:`app.s3df.status_registry`.

The upstream microservice owns the polling, store, and incident
lifecycle — this adapter is a stateless translation layer between
``ResourceStatus``/``StatusEvent``/``Incident`` records and the IRI
``Resource``/``Event``/``Incident`` models.
"""

import datetime
import logging
import uuid

from app.routers.status import facility_adapter as status_adapter
from app.routers.status import models as status_models
from app.s3df.clients import S3DFStatusApiError, get_s3df_status_api_client
from app.s3df.status_registry import (
    S3DF_RESOURCES,
    ResourceMeta,
    parse_status,
    site_id,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _paginate(items: list, offset: int | None, limit: int | None) -> list:
    if offset and offset > 0:
        items = items[offset:]
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def _parse_dt(value) -> datetime.datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _build_resource(meta: ResourceMeta, status_payload: dict | None) -> status_models.Resource:
    """Merge static metadata with a ResourceStatus dict from s3df-status-api."""
    last_modified = _utc_now()
    current_status = status_models.Status.unknown
    if status_payload is not None:
        current_status = parse_status(status_payload.get("status")) or status_models.Status.unknown
        last_modified = (
            _parse_dt(status_payload.get("last_changed_at"))
            or _parse_dt(status_payload.get("last_poll"))
            or last_modified
        )
    return status_models.Resource(
        id=meta.id,
        name=meta.name,
        description=meta.description,
        site_id=site_id(),
        group=meta.group,
        resource_type=meta.resource_type,
        capability_ids=list(meta.capability_ids),
        current_status=current_status,
        last_modified=last_modified,
    )


def _event_id(payload: dict) -> str:
    eid = payload.get("id")
    if eid is not None:
        return str(eid)
    # synthesize a stable id when the upstream omits one
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"s3df-event:{payload.get('resource_id')}:{payload.get('occurred_at')}",
    ).hex


def _build_event(payload: dict) -> status_models.Event | None:
    occurred_at = _parse_dt(payload.get("occurred_at"))
    if occurred_at is None:
        return None
    to_status = parse_status(payload.get("to_status")) or status_models.Status.unknown
    from_status = payload.get("from_status") or "unknown"
    detail = payload.get("detail")
    resource_id = payload.get("resource_id") or ""
    eid = _event_id(payload)
    return status_models.Event(
        id=eid,
        name=f"{resource_id}: {from_status} -> {to_status.value}",
        description=detail,
        last_modified=occurred_at,
        occurred_at=occurred_at,
        status=to_status,
        resource_id=resource_id,
        incident_id=None,
    )


def _build_incident(payload: dict) -> status_models.Incident | None:
    opened_at = _parse_dt(payload.get("opened_at"))
    if opened_at is None:
        return None
    resolved_at = _parse_dt(payload.get("resolved_at"))
    summary = payload.get("summary")
    resource_id = payload.get("resource_id") or ""
    incident_id = str(payload.get("id") or "")
    if not incident_id:
        return None
    is_resolved = resolved_at is not None
    incident_status = status_models.Status.up if is_resolved else status_models.Status.down
    resolution = (
        status_models.Resolution.completed if is_resolved else status_models.Resolution.unresolved
    )
    return status_models.Incident(
        id=incident_id,
        name=f"{resource_id} incident",
        description=summary,
        last_modified=resolved_at or opened_at,
        status=incident_status,
        resource_ids=[resource_id] if resource_id else [],
        event_ids=[],
        start=opened_at,
        end=resolved_at,
        type=status_models.IncidentType.unplanned,
        resolution=resolution,
    )


class S3DFStatusAdapter(status_adapter.FacilityAdapter):
    """IRI status FacilityAdapter backed by the s3df-status-api microservice."""

    def __init__(self):
        self._client = get_s3df_status_api_client()

    # -- helpers -----------------------------------------------------------

    async def _all_resources(self) -> list[status_models.Resource]:
        try:
            statuses = await self._client.list_resource_statuses()
        except S3DFStatusApiError:
            logger.exception("s3df-status-api list_resource_statuses failed; returning unknown statuses")
            statuses = []
        by_id = {s.get("resource_id"): s for s in statuses if s.get("resource_id")}
        return [_build_resource(meta, by_id.get(meta.id)) for meta in S3DF_RESOURCES.values()]

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
        resources = await self._all_resources()
        items = status_models.Resource.find(
            resources,
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

    async def get_resource(self, id_: str) -> status_models.Resource | None:
        meta = S3DF_RESOURCES.get(id_)
        if meta is None:
            # allow lookup-by-name as get_resources does (router supports it)
            for candidate in S3DF_RESOURCES.values():
                if candidate.name == id_:
                    meta = candidate
                    break
        if meta is None:
            return None
        try:
            payload = await self._client.get_resource_status(meta.id)
        except S3DFStatusApiError:
            logger.exception(f"s3df-status-api get_resource_status({meta.id}) failed")
            payload = None
        return _build_resource(meta, payload)

    # -- events ------------------------------------------------------------

    async def _all_events(
        self,
        resource_id: str | None = None,
        since: datetime.datetime | None = None,
    ) -> list[status_models.Event]:
        try:
            payloads = await self._client.list_events(resource_id=resource_id, since=since)
        except S3DFStatusApiError:
            logger.exception("s3df-status-api list_events failed; returning empty list")
            return []
        events: list[status_models.Event] = []
        for p in payloads:
            ev = _build_event(p)
            if ev is not None:
                events.append(ev)
        return events

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
        events = await self._all_events(resource_id=resource_id, since=from_)
        items = status_models.Event.find(
            events,
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

    async def get_event(self, id_: str) -> status_models.Event | None:
        events = await self._all_events()
        return status_models.Event.find_by_id(events, id_)

    # -- incidents ---------------------------------------------------------

    async def _all_incidents(
        self,
        resource_id: str | None = None,
        state: str | None = None,
    ) -> list[status_models.Incident]:
        try:
            payloads = await self._client.list_incidents(resource_id=resource_id, state=state)
        except S3DFStatusApiError:
            logger.exception("s3df-status-api list_incidents failed; returning empty list")
            return []
        incidents: list[status_models.Incident] = []
        for p in payloads:
            inc = _build_incident(p)
            if inc is not None:
                incidents.append(inc)
        return incidents

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
        incidents = await self._all_incidents(resource_id=resource_id)
        items = status_models.Incident.find(
            incidents,
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

    async def get_incident(self, id_: str) -> status_models.Incident | None:
        incidents = await self._all_incidents()
        return status_models.Incident.find_by_id(incidents, id_)
