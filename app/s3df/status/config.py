"""
Configuration for the S3DF status adapter.

Holds the static, request-independent pieces of the adapter:

  * The health-check model (``Backend``, ``Condition``, ``HealthCheck``) — a
    declarative success-criterion mirroring the model ``status-pusher`` uses.
  * ``MonitoredResource`` — pairs an IRI ``Resource`` template with the health
    check that drives it (see the duplication note below).
  * ``REGISTRY`` — the set of S3DF resources surfaced by ``/status``.
  * ``StatusSettings`` — environment-driven configuration.

Avoiding Resource/ResourceDef duplication
------------------------------------------
A resource's stable identity/metadata (id, name, description, group,
resource_type, capability_ids) is declared exactly once, as an IRI
``Resource`` template inside ``MonitoredResource``. The runtime-owned fields
(``current_status``, ``last_modified``, ``site_id``) are placeholders here and
are overlaid by the store when it projects a live view, so the fields are never
re-listed in a parallel definition.

Required/optional env vars:
  S3DF_PROMETHEUS_URL       Prometheus base URL   (default https://prometheus.slac.stanford.edu)
  S3DF_INFLUXDB_URL         InfluxDB base URL     (default https://influxdb.slac.stanford.edu)
  S3DF_INFLUXDB_DB          InfluxDB database     (default telegraf)
  S3DF_STATUS_POLL_INTERVAL Seconds between polls (default 60)
  S3DF_STATUS_HTTP_TIMEOUT  Per-query timeout sec (default 15)
  S3DF_SITE_ID              Site id for resources (default s3df)
  S3DF_STATUS_TLS_VERIFY    true | false | <ca-bundle-path>  (default false)
"""

import datetime
import operator
import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.routers.status.models import Resource, ResourceType, Status


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# Placeholder timestamp baked into static Resource templates. The store is the
# authority for ``last_modified`` and overwrites this when projecting a view.
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


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
class MonitoredResource:
    """Pairs a static ``Resource`` template with the health check driving it.

    The ``Resource`` carries the resource's stable identity/metadata exactly
    once. Its dynamic fields (``current_status``, ``last_modified``,
    ``site_id``) are placeholders that the store overlays when it builds the
    live view, which is what lets us avoid a separate ``ResourceDef`` that would
    otherwise re-list the same fields.
    """

    resource: Resource
    health_check: HealthCheck


def _template(
    *,
    id: str,
    name: str,
    description: str,
    group: str,
    resource_type: ResourceType,
    capability_ids: tuple[str, ...] = (),
) -> Resource:
    """Build a static Resource template. Dynamic fields are placeholders the
    store overwrites (``current_status``, ``last_modified``, ``site_id``)."""
    return Resource(
        id=id,
        name=name,
        description=description,
        last_modified=_EPOCH,
        site_id="",  # overlaid by the store from settings.site_id
        group=group,
        resource_type=resource_type,
        current_status=Status.unknown,
        capability_ids=list(capability_ids),
    )


# The set of S3DF resources surfaced by /status. Queries mirror those exercised
# by status-pusher's live tests (see status-pusher/Makefile). Extend as needed.
REGISTRY: list[MonitoredResource] = [
    MonitoredResource(
        resource=_template(
            id="s3df-ssh-gateway",
            name="SSH Login Gateway",
            description="S3DF interactive SSH login gateway reachability.",
            group="access",
            resource_type=ResourceType.service,
        ),
        health_check=HealthCheck(
            backend=Backend.prometheus,
            query="avg( avg_over_time(nmap_port_state{service=`ssh`,group=`s3df`}[5m]) )",
            up_when=Condition("eq", 1.0),
        ),
    ),
    MonitoredResource(
        resource=_template(
            id="s3df-slurmctld",
            name="Slurm Controller (slurmctld)",
            description="Slurm workload manager controller daemon health.",
            group="compute",
            resource_type=ResourceType.compute,
        ),
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
    MonitoredResource(
        resource=_template(
            id="s3df-slurmdbd",
            name="Slurm DB Daemon (slurmdbd)",
            description="Slurm accounting database daemon health.",
            group="compute",
            resource_type=ResourceType.compute,
        ),
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
