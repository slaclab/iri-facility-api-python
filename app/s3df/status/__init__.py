"""
S3DF status adapter package.

Splits the adapter into focused modules:

  * ``config``         — health-check model, resource registry, settings
  * ``health_checker`` — Prometheus, InfluxDB, HTTP query + evaluation
  * ``store``          — in-memory current-status / event / incident store
  * ``poller``         — background polling loop

The public ``S3DFStatusAdapter`` lives in ``app.s3df.status_adapter``.
"""

from .config import (
    REGISTRY,
    Backend,
    Condition,
    HealthCheck,
    MonitoredResource,
    StatusSettings,
    build_registry,
)
from .health_checker import HealthChecker, HealthResult, aggregate_results, evaluate
from .poller import StatusPoller
from .store import StatusStore

__all__ = [
    "REGISTRY",
    "Backend",
    "Condition",
    "HealthCheck",
    "MonitoredResource",
    "StatusSettings",
    "build_registry",
    "HealthChecker",
    "HealthResult",
    "aggregate_results",
    "evaluate",
    "StatusPoller",
    "StatusStore",
]
