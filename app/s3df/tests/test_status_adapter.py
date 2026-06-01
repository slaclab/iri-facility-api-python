import datetime
import json

import httpx
import pytest

from app.routers.status.models import ResourceType, Status
from app.s3df.status.config import (
    Backend,
    Condition,
    HealthCheck,
    MonitoredResource,
    REGISTRY,
    RESOURCE_TEMPLATES,
    StatusSettings,
    build_registry,
)
from app.s3df.status.health_checker import HealthChecker, HealthResult, aggregate_results
from app.s3df.status.store import StatusStore
from app.s3df.status_adapter import S3DFStatusAdapter


EXPECTED_NAMES = [
    "SSH Bastions",
    "Interactive Nodes",
    "S3DF Docs",
    "Batch Servers",
    "Slurm",
    "Monitoring",
    "Coact",
    "OnDemand",
    "Kubernetes",
    "Storage",
    "DTNs",
]

EXPECTED_IDS = [
    "s3df-ssh-bastions",
    "s3df-interactive-nodes",
    "s3df-docs",
    "s3df-batch-servers",
    "s3df-slurm",
    "s3df-monitoring",
    "s3df-coact",
    "s3df-ondemand",
    "s3df-kubernetes",
    "s3df-storage",
    "s3df-dtns",
]

NOW = datetime.datetime(2026, 6, 1, 13, 0, tzinfo=datetime.timezone.utc)


def _settings(monkeypatch: pytest.MonkeyPatch) -> StatusSettings:
    monkeypatch.delenv("S3DF_STATUS_CHECKS_JSON", raising=False)
    settings = StatusSettings()
    settings.prometheus_url = "https://prometheus.example"
    settings.influxdb_url = "https://influx.example"
    settings.poll_interval = 3600
    settings.http_timeout = 1
    return settings


def test_registry_matches_s3df_status_resources():
    assert [m.resource.name for m in REGISTRY] == EXPECTED_NAMES
    assert [m.resource.id for m in REGISTRY] == EXPECTED_IDS
    assert {m.resource.id for m in REGISTRY}.isdisjoint({"s3df-ssh-gateway", "s3df-slurmctld", "s3df-slurmdbd"})

    checks_by_id = {m.resource.id: m.health_checks for m in REGISTRY}
    assert [check.backend for check in checks_by_id["s3df-ssh-bastions"]] == [Backend.prometheus]
    assert [check.backend for check in checks_by_id["s3df-slurm"]] == [Backend.influxdb, Backend.influxdb]
    assert all(not checks for id_, checks in checks_by_id.items() if id_ not in {"s3df-ssh-bastions", "s3df-slurm"})


def test_status_log_runtime_configuration_is_not_available(monkeypatch):
    settings = _settings(monkeypatch)

    assert "status_log" not in Backend.__members__
    assert not hasattr(settings, "status_logs_base_url")
    assert not hasattr(settings, "status_log_max_age")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (200, Status.up),
        (499, Status.degraded),
        (500, Status.down),
        (None, Status.unknown),
    ],
)
def test_condition_evaluation_with_degraded_status(value, expected):
    check = HealthCheck(
        backend=Backend.http,
        url="https://example/status",
        up_when=Condition("eq", 200),
        degraded_when=Condition("lt", 500),
    )

    from app.s3df.status.health_checker import evaluate

    assert evaluate(check, value) == expected


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([], Status.unknown),
        ([HealthResult(Status.unknown, None, NOW, "backend unavailable")], Status.unknown),
        ([HealthResult(Status.up, 1.0, NOW), HealthResult(Status.unknown, None, NOW, "timeout")], Status.up),
        ([HealthResult(Status.up, 1.0, NOW), HealthResult(Status.degraded, 0.5, NOW)], Status.degraded),
        ([HealthResult(Status.up, 1.0, NOW), HealthResult(Status.down, 0.0, NOW)], Status.down),
    ],
)
def test_aggregate_results(results, expected):
    assert aggregate_results(results).status == expected


@pytest.mark.asyncio
async def test_health_checker_queries_prometheus(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query"
        assert request.url.params["query"] == "up"
        return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [0, "1"]}]}})

    settings = _settings(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HealthChecker(settings, client).check(
            (HealthCheck(backend=Backend.prometheus, query="up", up_when=Condition("eq", 1)),)
        )

    assert result.status == Status.up


@pytest.mark.asyncio
async def test_health_checker_queries_influxdb(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/query"
        assert request.url.params["db"] == "telegraf"
        assert request.url.params["q"] == "SELECT mean(status_code)"
        return httpx.Response(200, json={"results": [{"series": [{"values": [["2026-06-01T12:00:00Z", 0.5]]}]}]})

    settings = _settings(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HealthChecker(settings, client).check(
            (
                HealthCheck(
                    backend=Backend.influxdb,
                    query="SELECT mean(status_code)",
                    up_when=Condition("eq", 1),
                    degraded_when=Condition("gte", 0.5),
                ),
            )
        )

    assert result.status == Status.degraded


@pytest.mark.asyncio
async def test_health_checker_queries_http_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://docs.example/status"
        assert request.headers["x-check"] == "docs"
        return httpx.Response(204)

    settings = _settings(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HealthChecker(settings, client).check(
            (
                HealthCheck(
                    backend=Backend.http,
                    url="https://docs.example/status",
                    headers={"x-check": "docs"},
                    up_when=Condition("eq", 204),
                ),
            )
        )

    assert result.status == Status.up


@pytest.mark.asyncio
async def test_health_checker_maps_backend_failures_to_unknown(monkeypatch):
    settings = _settings(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        result = await HealthChecker(settings, client).check(
            (HealthCheck(backend=Backend.prometheus, query="up", up_when=Condition("eq", 1)),)
        )

    assert result.status == Status.unknown
    assert result.error is not None


def test_configured_checks_are_merged_with_builtin_checks(monkeypatch):
    monkeypatch.setenv(
        "S3DF_STATUS_CHECKS_JSON",
        json.dumps(
            {
                "s3df-docs": [
                    {
                        "backend": "http",
                        "name": "docs-health",
                        "url": "https://docs.example/status",
                        "up_when": {"comparator": "eq", "value": 204},
                    }
                ],
                "s3df-slurm": [
                    {
                        "backend": "prometheus",
                        "name": "slurm-exporter",
                        "query": "slurm_up",
                        "up_when": {"comparator": "gte", "value": 1},
                    }
                ],
            }
        ),
    )

    registry = build_registry(StatusSettings())
    checks_by_id = {m.resource.id: m.health_checks for m in registry}

    assert checks_by_id["s3df-docs"][0].backend == Backend.http
    assert checks_by_id["s3df-docs"][0].up_when == Condition("eq", 204)
    assert [check.name for check in checks_by_id["s3df-slurm"]] == [
        "slurmctld-process",
        "slurmdbd-process",
        "slurm-exporter",
    ]


def test_invalid_configured_check_resource_id_is_explicit(monkeypatch):
    monkeypatch.setenv("S3DF_STATUS_CHECKS_JSON", json.dumps({"s3df-missing": []}))

    with pytest.raises(ValueError, match="Unknown S3DF status resource id"):
        StatusSettings()


@pytest.mark.asyncio
async def test_adapter_returns_cached_resources_with_current_status_and_filters(monkeypatch):
    observed_at = datetime.datetime(2026, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)
    statuses = {
        "s3df-kubernetes": Status.degraded,
        "s3df-storage": Status.down,
    }

    monitored = [
        MonitoredResource(
            resource=resource,
            health_checks=(HealthCheck(backend=Backend.http, name=resource.id, url=f"https://status.example/{resource.id}"),),
        )
        for resource in RESOURCE_TEMPLATES
    ]

    async def fake_check(self, checks):
        return HealthResult(statuses.get(checks[0].name, Status.up), None, observed_at)

    monkeypatch.setattr(HealthChecker, "check", fake_check)

    adapter = S3DFStatusAdapter(settings=_settings(monkeypatch), monitored=monitored)
    try:
        resources = await adapter.get_resources(0, 100)

        assert [r.name for r in resources] == EXPECTED_NAMES
        assert [r.id for r in resources] == EXPECTED_IDS
        assert {r.current_status for r in resources} == {Status.up, Status.degraded, Status.down}
        assert all(r.last_modified == observed_at for r in resources)
        assert [r.name for r in await adapter.get_resources(0, 100, current_status=Status.down)] == ["Storage"]
        assert [r.name for r in await adapter.get_resources(0, 100, group="access")] == ["SSH Bastions", "OnDemand"]
        assert [r.name for r in await adapter.get_resources(0, 100, resource_type=ResourceType.website)] == ["S3DF Docs", "OnDemand"]
        assert len(await adapter.get_resources(0, 100, site_id="s3df")) == len(EXPECTED_NAMES)
        assert (await adapter.get_resource("Storage")).id == "s3df-storage"
    finally:
        await adapter.aclose()


def test_store_incident_lifecycle_with_cached_check_results():
    resource = REGISTRY[0].resource
    store = StatusStore("s3df", [resource])
    ts1 = datetime.datetime(2026, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)
    ts2 = datetime.datetime(2026, 6, 1, 12, 5, tzinfo=datetime.timezone.utc)
    ts3 = datetime.datetime(2026, 6, 1, 12, 10, tzinfo=datetime.timezone.utc)
    ts4 = datetime.datetime(2026, 6, 1, 12, 15, tzinfo=datetime.timezone.utc)

    store.record(resource.id, HealthResult(Status.up, None, ts1))
    assert len(store.events()) == 1
    assert store.incidents() == []

    store.record(resource.id, HealthResult(Status.down, None, ts2))
    incident = store.incidents()[0]
    assert incident.status == Status.down
    assert incident.resolution.value == "unresolved"
    assert incident.start == ts2
    assert store.events()[-1].incident_id == incident.id

    store.record(resource.id, HealthResult(Status.unknown, None, ts3))
    assert store.incidents()[0].id == incident.id
    assert store.incidents()[0].end is None
    assert store.events()[-1].status == Status.unknown
    assert store.events()[-1].incident_id is None

    store.record(resource.id, HealthResult(Status.up, None, ts4))
    closed = store.incidents()[0]
    assert closed.id == incident.id
    assert closed.status == Status.up
    assert closed.end == ts4
    assert closed.resolution.value == "completed"
    assert store.events()[-1].incident_id == incident.id
