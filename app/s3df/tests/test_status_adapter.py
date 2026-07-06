"""Tests for the S3DF status adapter (microservice-backed)."""

import datetime
from unittest.mock import AsyncMock

import pytest

from app.routers.status import models as status_models
from app.s3df import status_adapter as status_adapter_module
from app.s3df.clients import S3DFStatusApiError
from app.s3df.status_adapter import S3DFStatusAdapter
from app.s3df.status_registry import S3DF_RESOURCES


def _statuses_payload(overrides: dict[str, str] | None = None) -> list[dict]:
    """Build a synthetic ResourceStatus list covering every registry id."""
    overrides = overrides or {}
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc).isoformat()
    return [
        {
            "resource_id": rid,
            "status": overrides.get(rid, "up"),
            "last_changed_at": now,
            "last_poll": now,
            "check_results": [],
        }
        for rid in S3DF_RESOURCES
    ]


def _make_adapter(monkeypatch, *, list_resources=None, get_resource=None, list_events=None, list_incidents=None):
    """Construct an adapter wired to a mock client."""
    mock_client = AsyncMock()
    mock_client.list_resource_statuses = AsyncMock(return_value=list_resources or _statuses_payload())
    mock_client.get_resource_status = AsyncMock(side_effect=get_resource)
    mock_client.list_events = AsyncMock(return_value=list_events or [])
    mock_client.list_incidents = AsyncMock(return_value=list_incidents or [])
    monkeypatch.setattr(status_adapter_module, "get_s3df_status_api_client", lambda: mock_client)
    return S3DFStatusAdapter(), mock_client


@pytest.mark.asyncio
async def test_get_resources_merges_registry_and_status(monkeypatch):
    adapter, _ = _make_adapter(monkeypatch)
    resources = await adapter.get_resources(offset=0, limit=100)
    assert len(resources) == len(S3DF_RESOURCES)
    by_id = {r.id: r for r in resources}
    ada = by_id["ada"]
    assert ada.name == "Batch (ada)"
    assert ada.group == "compute"
    assert ada.resource_type is status_models.ResourceType.compute
    assert ada.current_status is status_models.Status.up


@pytest.mark.asyncio
async def test_get_resources_filter_by_group(monkeypatch):
    adapter, _ = _make_adapter(monkeypatch)
    resources = await adapter.get_resources(offset=0, limit=100, group="compute")
    ids = {r.id for r in resources}
    assert ids == {"ada", "ampere", "turing", "milano", "torino", "roma", "hopper"}


@pytest.mark.asyncio
async def test_get_resources_filter_by_current_status(monkeypatch):
    payload = _statuses_payload({"sdfhome": "down", "sdfdata": "degraded"})
    adapter, _ = _make_adapter(monkeypatch, list_resources=payload)
    down = await adapter.get_resources(offset=0, limit=100, current_status=status_models.Status.down)
    assert [r.id for r in down] == ["sdfhome"]


@pytest.mark.asyncio
async def test_get_resources_falls_back_when_api_fails(monkeypatch):
    async def boom(*_, **__):
        raise S3DFStatusApiError("boom")

    adapter = S3DFStatusAdapter.__new__(S3DFStatusAdapter)
    adapter._client = AsyncMock()
    adapter._client.list_resource_statuses = AsyncMock(side_effect=boom)
    resources = await adapter.get_resources(offset=0, limit=100)
    assert len(resources) == len(S3DF_RESOURCES)
    assert all(r.current_status is status_models.Status.unknown for r in resources)


@pytest.mark.asyncio
async def test_get_resource_unknown_returns_none(monkeypatch):
    adapter, _ = _make_adapter(monkeypatch, get_resource=lambda *_a, **_k: None)
    assert await adapter.get_resource("not-a-real-id") is None


@pytest.mark.asyncio
async def test_get_resource_known_id(monkeypatch):
    async def get_resource(_id):
        return {
            "resource_id": "ada",
            "status": "degraded",
            "last_changed_at": "2026-06-01T12:00:00Z",
            "last_poll": "2026-06-01T12:00:00Z",
            "check_results": [],
        }

    adapter = S3DFStatusAdapter.__new__(S3DFStatusAdapter)
    adapter._client = AsyncMock()
    adapter._client.get_resource_status = AsyncMock(side_effect=get_resource)
    res = await adapter.get_resource("ada")
    assert res is not None
    assert res.id == "ada"
    assert res.current_status is status_models.Status.degraded


@pytest.mark.asyncio
async def test_get_events_returns_empty_when_disabled(monkeypatch):
    adapter, _ = _make_adapter(monkeypatch, list_events=[])
    assert await adapter.get_events(offset=0, limit=100) == []


@pytest.mark.asyncio
async def test_get_events_maps_payloads(monkeypatch):
    payloads = [
        {
            "id": 1,
            "resource_id": "s3df-coact",
            "from_status": "up",
            "to_status": "down",
            "occurred_at": "2026-06-01T12:00:00Z",
            "detail": "probe failed",
        }
    ]
    adapter, _ = _make_adapter(monkeypatch, list_events=payloads)
    events = await adapter.get_events(offset=0, limit=100)
    assert len(events) == 1
    e = events[0]
    assert e.id == "1"
    assert e.status is status_models.Status.down
    assert e.resource_id == "s3df-coact"
    assert e.description == "probe failed"


@pytest.mark.asyncio
async def test_get_incidents_maps_open_and_resolved(monkeypatch):
    payloads = [
        {
            "id": "inc-1",
            "resource_id": "s3df-storage",
            "opened_at": "2026-06-01T12:00:00Z",
            "resolved_at": None,
            "summary": "weka degraded",
        },
        {
            "id": "inc-2",
            "resource_id": "s3df-coact",
            "opened_at": "2026-06-01T11:00:00Z",
            "resolved_at": "2026-06-01T11:30:00Z",
            "summary": "transient",
        },
    ]
    adapter, _ = _make_adapter(monkeypatch, list_incidents=payloads)
    incidents = await adapter.get_incidents(offset=0, limit=100)
    assert len(incidents) == 2
    by_id = {i.id: i for i in incidents}
    assert by_id["inc-1"].resolution is status_models.Resolution.unresolved
    assert by_id["inc-1"].status is status_models.Status.down
    assert by_id["inc-2"].resolution is status_models.Resolution.completed
    assert by_id["inc-2"].status is status_models.Status.up


@pytest.mark.asyncio
async def test_get_incidents_returns_empty_when_disabled(monkeypatch):
    adapter, _ = _make_adapter(monkeypatch, list_incidents=[])
    assert await adapter.get_incidents(offset=0, limit=100) == []
