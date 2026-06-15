"""
Static metadata for the S3DF resources monitored by ``s3df-status-api``.

The upstream microservice only exposes ``ResourceStatus`` records (id +
current status). The IRI ``/status/resources`` endpoint, in contrast,
must surface the full ``Resource`` model (id, name, description, group,
resource_type, site_id). This module ships a static map so the IRI
adapter can merge dynamic status with descriptive metadata.

Mirror of ``s3df-status-api/resources.yaml``.
"""

from dataclasses import dataclass, field

from app.routers.status import models as status_models
from app.s3df.config import settings


@dataclass(frozen=True)
class ResourceMeta:
    """Static metadata for an S3DF status resource."""
    id: str
    name: str
    description: str
    group: str
    resource_type: status_models.ResourceType
    capability_ids: list[str] = field(default_factory=list)


# 11 resources, ids/names/groups from s3df-status-api/resources.yaml.
S3DF_RESOURCES: dict[str, ResourceMeta] = {
    meta.id: meta
    for meta in (
        ResourceMeta(
            id="s3df-ssh-bastions",
            name="SSH Bastions",
            description="S3DF SSH bastion hosts for command-line access.",
            group="access",
            resource_type=status_models.ResourceType.service,
        ),
        ResourceMeta(
            id="s3df-interactive-nodes",
            name="Interactive Nodes",
            description="S3DF interactive login and analysis nodes.",
            group="compute",
            resource_type=status_models.ResourceType.compute,
        ),
        ResourceMeta(
            id="s3df-docs",
            name="S3DF Docs",
            description="S3DF user documentation site.",
            group="documentation",
            resource_type=status_models.ResourceType.website,
        ),
        ResourceMeta(
            id="s3df-batch-servers",
            name="Batch Servers",
            description="S3DF batch submission and scheduling servers.",
            group="compute",
            resource_type=status_models.ResourceType.compute,
        ),
        ResourceMeta(
            id="s3df-slurm",
            name="Slurm",
            description="S3DF Slurm workload management service.",
            group="compute",
            resource_type=status_models.ResourceType.service,
        ),
        ResourceMeta(
            id="s3df-monitoring",
            name="Monitoring",
            description="S3DF monitoring and observability services.",
            group="operations",
            resource_type=status_models.ResourceType.service,
        ),
        ResourceMeta(
            id="s3df-coact",
            name="Coact",
            description="S3DF Coact allocation and account service.",
            group="accounts",
            resource_type=status_models.ResourceType.service,
        ),
        ResourceMeta(
            id="s3df-ondemand",
            name="OnDemand",
            description="S3DF Open OnDemand web service.",
            group="access",
            resource_type=status_models.ResourceType.website,
        ),
        ResourceMeta(
            id="s3df-kubernetes",
            name="Kubernetes",
            description="S3DF Kubernetes platform.",
            group="platform",
            resource_type=status_models.ResourceType.system,
        ),
        ResourceMeta(
            id="s3df-storage",
            name="Storage",
            description="S3DF storage services.",
            group="storage",
            resource_type=status_models.ResourceType.storage,
        ),
        ResourceMeta(
            id="s3df-dtns",
            name="DTNs",
            description="S3DF data transfer nodes.",
            group="data-transfer",
            resource_type=status_models.ResourceType.network,
        ),
    )
}


def site_id() -> str:
    """The site id used for every S3DF status resource."""
    return settings.facility_name or "s3df"


_STATUS_MAP = {
    "up": status_models.Status.up,
    "down": status_models.Status.down,
    "degraded": status_models.Status.degraded,
    "unknown": status_models.Status.unknown,
}


def parse_status(value: str | None) -> status_models.Status | None:
    """Map an upstream status string to the IRI Status enum."""
    if value is None:
        return None
    return _STATUS_MAP.get(value, status_models.Status.unknown)
