"""
Configuration for the S3DF status adapter.

IRI owns the S3DF status runtime: it periodically runs configured health checks,
evaluates status-pusher-style conditions, and caches the latest result for each
resource. The external S3DF status repositories are reference material only; this
module does not fetch dashboard log files.

Required/optional env vars:
  S3DF_PROMETHEUS_URL        Prometheus base URL   (default https://prometheus.slac.stanford.edu)
  S3DF_INFLUXDB_URL          InfluxDB base URL     (default https://influxdb.slac.stanford.edu)
  S3DF_INFLUXDB_DB           InfluxDB database     (default telegraf)
  S3DF_STATUS_CHECKS_FILE    JSON file with resource health checks
  S3DF_STATUS_CHECKS_JSON    JSON mapping resource ids to additional health checks
  S3DF_STATUS_REQUIRE_FULL_COVERAGE  Require every resource to have checks (default true)
  S3DF_STATUS_POLL_INTERVAL  Seconds between polls (default 60)
  S3DF_STATUS_HTTP_TIMEOUT   Per-query timeout sec (default 15)
  S3DF_SITE_ID               Site id for resources (default s3df)
  S3DF_STATUS_TLS_VERIFY     true | false | <ca-bundle-path>  (default false)
"""

import datetime
import json
import operator
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from app.routers.status.models import Resource, ResourceType, Status


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


class Backend(str, Enum):
    """Backend/source a health check queries."""

    prometheus = "prometheus"
    influxdb = "influxdb"
    http = "http"


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
    """A comparison of an observed value against a threshold."""

    comparator: str
    value: float

    def __post_init__(self) -> None:
        if self.comparator not in _COMPARATORS:
            raise ValueError(f"Unknown comparator: {self.comparator}")

    def met(self, observed: float) -> bool:
        return bool(_COMPARATORS[self.comparator](observed, self.value))


@dataclass(frozen=True)
class HealthCheck:
    """How to determine a resource's status from an IRI-owned source."""

    backend: Backend
    name: str | None = None
    query: str | None = None
    up_when: Condition = field(default_factory=lambda: Condition("eq", 1.0))
    db_name: str | None = None
    degraded_when: Condition | None = None
    url: str | None = None
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    follow_redirects: bool = True


@dataclass(frozen=True)
class MonitoredResource:
    """Pairs a static Resource template with the health checks driving it."""

    resource: Resource
    health_checks: tuple[HealthCheck, ...] = ()


def _template(
    *,
    id: str,
    name: str,
    description: str,
    group: str,
    resource_type: ResourceType,
    capability_ids: tuple[str, ...] = (),
) -> Resource:
    """Build a static Resource template. The store overwrites dynamic fields."""
    return Resource(
        id=id,
        name=name,
        description=description,
        last_modified=_EPOCH,
        site_id="",
        group=group,
        resource_type=resource_type,
        current_status=Status.unknown,
        capability_ids=list(capability_ids),
    )


RESOURCE_TEMPLATES: tuple[Resource, ...] = (
    _template(
        id="s3df-ssh-bastions",
        name="SSH Bastions",
        description="S3DF SSH bastion hosts for command-line access.",
        group="access",
        resource_type=ResourceType.service,
    ),
    _template(
        id="s3df-interactive-nodes",
        name="Interactive Nodes",
        description="S3DF interactive login and analysis nodes.",
        group="compute",
        resource_type=ResourceType.compute,
    ),
    _template(
        id="s3df-docs",
        name="S3DF Docs",
        description="S3DF user documentation site.",
        group="documentation",
        resource_type=ResourceType.website,
    ),
    _template(
        id="s3df-batch-servers",
        name="Batch Servers",
        description="S3DF batch submission and scheduling servers.",
        group="compute",
        resource_type=ResourceType.compute,
    ),
    _template(
        id="s3df-slurm",
        name="Slurm",
        description="S3DF Slurm workload management service.",
        group="compute",
        resource_type=ResourceType.service,
    ),
    _template(
        id="s3df-monitoring",
        name="Monitoring",
        description="S3DF monitoring and observability services.",
        group="operations",
        resource_type=ResourceType.service,
    ),
    _template(
        id="s3df-coact",
        name="Coact",
        description="S3DF Coact allocation and account service.",
        group="accounts",
        resource_type=ResourceType.service,
    ),
    _template(
        id="s3df-ondemand",
        name="OnDemand",
        description="S3DF Open OnDemand web service.",
        group="access",
        resource_type=ResourceType.website,
    ),
    _template(
        id="s3df-kubernetes",
        name="Kubernetes",
        description="S3DF Kubernetes platform.",
        group="platform",
        resource_type=ResourceType.system,
    ),
    _template(
        id="s3df-storage",
        name="Storage",
        description="S3DF storage services.",
        group="storage",
        resource_type=ResourceType.storage,
    ),
    _template(
        id="s3df-dtns",
        name="DTNs",
        description="S3DF data transfer nodes.",
        group="data-transfer",
        resource_type=ResourceType.network,
    ),
)

RESOURCE_IDS = {resource.id for resource in RESOURCE_TEMPLATES}

BUILTIN_CHECKS: dict[str, tuple[HealthCheck, ...]] = {
    "s3df-ssh-bastions": (
        HealthCheck(
            backend=Backend.prometheus,
            name="ssh-bastion-port-state",
            query="avg( avg_over_time(nmap_port_state{service=`ssh`,group=`s3df`}[5m]) )",
            up_when=Condition("eq", 1.0),
        ),
    ),
    "s3df-slurm": (
        HealthCheck(
            backend=Backend.influxdb,
            name="slurmctld-process",
            db_name="telegraf",
            query=(
                'SELECT mean("status_code") FROM "monit_process" '
                "WHERE \"service\" = 'slurmctld' AND time > now()-5m"
            ),
            up_when=Condition("eq", 1.0),
        ),
        HealthCheck(
            backend=Backend.influxdb,
            name="slurmdbd-process",
            db_name="telegraf",
            query=(
                'SELECT mean("status_code") FROM "monit_process" '
                "WHERE \"service\" = 'slurmdbd' AND time > now()-5m"
            ),
            up_when=Condition("eq", 1.0),
        ),
    ),
}


def build_registry(
    settings: "StatusSettings | None" = None,
    *,
    require_full_coverage: bool | None = None,
) -> list[MonitoredResource]:
    """Build monitored resources with built-in plus configured checks."""
    configured = settings.resource_checks if settings is not None else {}
    monitored = [
        MonitoredResource(
            resource=resource,
            health_checks=BUILTIN_CHECKS.get(resource.id, ()) + configured.get(resource.id, ()),
        )
        for resource in RESOURCE_TEMPLATES
    ]
    _validate_unique_check_names(monitored)

    strict = require_full_coverage
    if strict is None:
        strict = settings.require_full_coverage if settings is not None else False
    if strict:
        _validate_full_coverage(monitored)

    return monitored


def missing_check_resources(monitored: list[MonitoredResource]) -> list[MonitoredResource]:
    """Return canonical resources that have no configured health checks."""
    return [resource for resource in monitored if not resource.health_checks]


def _validate_full_coverage(monitored: list[MonitoredResource]) -> None:
    missing = missing_check_resources(monitored)
    if not missing:
        return
    labels = ", ".join(f"{m.resource.id} ({m.resource.name})" for m in missing)
    raise ValueError(
        "S3DF status resources missing health checks: "
        f"{labels}. Configure checks with S3DF_STATUS_CHECKS_FILE or "
        "S3DF_STATUS_CHECKS_JSON, or set S3DF_STATUS_REQUIRE_FULL_COVERAGE=false "
        "only for development/test use."
    )


def _validate_unique_check_names(monitored: list[MonitoredResource]) -> None:
    for resource in monitored:
        seen: set[str] = set()
        for check in resource.health_checks:
            if check.name is None:
                continue
            if check.name in seen:
                raise ValueError(
                    f"Duplicate S3DF status check name for {resource.resource.id}: {check.name}"
                )
            seen.add(check.name)


# Static default registry for tests/importers that do not need env-driven checks.
REGISTRY: list[MonitoredResource] = build_registry()


class StatusSettings:
    """Environment-driven settings for the S3DF status adapter."""

    def __init__(self) -> None:
        self.prometheus_url = os.getenv("S3DF_PROMETHEUS_URL", "https://prometheus.slac.stanford.edu")
        self.influxdb_url = os.getenv("S3DF_INFLUXDB_URL", "https://influxdb.slac.stanford.edu")
        self.influxdb_db = os.getenv("S3DF_INFLUXDB_DB", "telegraf")
        self.require_full_coverage = self._parse_bool(
            os.getenv("S3DF_STATUS_REQUIRE_FULL_COVERAGE", "true"),
            "S3DF_STATUS_REQUIRE_FULL_COVERAGE",
        )
        self.resource_checks = self._load_resource_checks()
        self.poll_interval = int(os.getenv("S3DF_STATUS_POLL_INTERVAL", "60"))
        self.http_timeout = float(os.getenv("S3DF_STATUS_HTTP_TIMEOUT", "15"))
        # NOTE: the /facility adapter currently mints a random site uuid per
        # process, so this id is not a guaranteed cross-reference yet. Set
        # S3DF_SITE_ID once a stable site identifier is established.
        self.site_id = os.getenv("S3DF_SITE_ID", "s3df")
        self.tls_verify = self._parse_verify(os.getenv("S3DF_STATUS_TLS_VERIFY", "false"))

    @staticmethod
    def _parse_bool(raw: str, name: str) -> bool:
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
        raise ValueError(f"{name} must be a boolean value")

    @staticmethod
    def _parse_verify(raw: str) -> bool | str:
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
        return raw

    @classmethod
    def _load_resource_checks(cls) -> dict[str, tuple[HealthCheck, ...]]:
        checks_file = os.getenv("S3DF_STATUS_CHECKS_FILE")
        file_checks = cls._parse_resource_checks_file(checks_file)
        json_checks = cls._parse_resource_checks(
            os.getenv("S3DF_STATUS_CHECKS_JSON", "{}"),
            "S3DF_STATUS_CHECKS_JSON",
        )
        return cls._merge_resource_checks(file_checks, json_checks)

    @classmethod
    def _parse_resource_checks_file(cls, checks_file: str | None) -> dict[str, tuple[HealthCheck, ...]]:
        if not checks_file:
            return {}
        path = Path(checks_file)
        try:
            raw = path.read_text()
        except OSError as exc:
            raise ValueError(f"S3DF_STATUS_CHECKS_FILE could not be read: {checks_file}") from exc
        return cls._parse_resource_checks(raw, f"S3DF_STATUS_CHECKS_FILE ({checks_file})")

    @staticmethod
    def _merge_resource_checks(
        *sources: dict[str, tuple[HealthCheck, ...]],
    ) -> dict[str, tuple[HealthCheck, ...]]:
        merged: dict[str, tuple[HealthCheck, ...]] = {}
        for source in sources:
            for resource_id, checks in source.items():
                merged[resource_id] = merged.get(resource_id, ()) + checks
        return merged

    @classmethod
    def _parse_resource_checks(cls, raw: str, source: str) -> dict[str, tuple[HealthCheck, ...]]:
        raw = raw.strip()
        if not raw:
            return {}
        data = cls._loads_json_object(raw, source)
        if not isinstance(data, dict):
            raise ValueError(f"{source} must be an object keyed by resource id")

        parsed: dict[str, tuple[HealthCheck, ...]] = {}
        for resource_id, checks in data.items():
            if resource_id not in RESOURCE_IDS:
                raise ValueError(f"Unknown S3DF status resource id in {source}: {resource_id}")
            if not isinstance(checks, list):
                raise ValueError(f"Checks for {resource_id} must be a list")
            parsed[resource_id] = tuple(
                cls._parse_check(resource_id, idx, check) for idx, check in enumerate(checks)
            )
        return parsed

    @staticmethod
    def _loads_json_object(raw: str, source: str) -> Any:
        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            data: dict[str, Any] = {}
            for key, value in pairs:
                if key in data:
                    raise ValueError(f"{source} contains duplicate key: {key}")
                data[key] = value
            return data

        try:
            return json.loads(raw, object_pairs_hook=reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source} must be valid JSON") from exc

    @classmethod
    def _parse_check(cls, resource_id: str, idx: int, raw: Any) -> HealthCheck:
        if not isinstance(raw, dict):
            raise ValueError(f"Check {idx} for {resource_id} must be an object")
        try:
            backend = Backend(raw["backend"])
        except KeyError as exc:
            raise ValueError(f"Check {idx} for {resource_id} is missing backend") from exc
        except ValueError as exc:
            raise ValueError(
                f"Check {idx} for {resource_id} has unsupported backend: {raw.get('backend')}"
            ) from exc

        up_when = cls._parse_condition(
            raw.get("up_when"),
            default=Condition("eq", 200.0 if backend == Backend.http else 1.0),
        )
        degraded_when = cls._parse_condition(raw.get("degraded_when"), default=None)
        name = cls._optional_str(
            raw.get("name"),
            f"Check {idx} for {resource_id} has invalid name",
        )
        query = cls._optional_str(
            raw.get("query"),
            f"Check {idx} for {resource_id} has invalid query",
        )
        db_name = cls._optional_str(
            raw.get("db_name"),
            f"Check {idx} for {resource_id} has invalid db_name",
        )
        url = cls._optional_str(
            raw.get("url"),
            f"Check {idx} for {resource_id} has invalid url",
        )
        method = raw.get("method", "GET")
        if not isinstance(method, str) or not method.strip():
            raise ValueError(f"Check {idx} for {resource_id} has invalid method")
        follow_redirects = raw.get("follow_redirects", True)
        if not isinstance(follow_redirects, bool):
            raise ValueError(f"Check {idx} for {resource_id} has invalid follow_redirects")
        headers = raw.get("headers", {})
        if headers is None:
            headers = {}
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError(f"Check {idx} for {resource_id} has invalid headers")

        check = HealthCheck(
            backend=backend,
            name=name,
            query=query,
            up_when=up_when,
            db_name=db_name,
            degraded_when=degraded_when,
            url=url,
            method=method.strip().upper(),
            headers=headers,
            follow_redirects=follow_redirects,
        )
        cls._validate_check(resource_id, idx, check)
        return check

    @staticmethod
    def _optional_str(value: Any, error: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(error)
        return value

    @staticmethod
    def _parse_condition(raw: Any, default: Condition | None) -> Condition | None:
        if raw is None:
            return default
        if not isinstance(raw, dict):
            raise ValueError("condition must be an object")
        try:
            comparator = raw["comparator"]
            value = raw["value"]
        except KeyError as exc:
            raise ValueError("condition requires comparator and value") from exc
        return Condition(str(comparator), float(value))

    @staticmethod
    def _validate_check(resource_id: str, idx: int, check: HealthCheck) -> None:
        if check.backend in (Backend.prometheus, Backend.influxdb) and not check.query:
            raise ValueError(f"Check {idx} for {resource_id} requires query")
        if check.backend == Backend.http and not check.url:
            raise ValueError(f"Check {idx} for {resource_id} requires url")
