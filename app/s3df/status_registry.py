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
    resource_type: status_models.ResourceTypeValue
    capability_ids: list[str] = field(default_factory=list)
    supported_endpoints: tuple[status_models.Endpoint, ...] = ()


# 11 resources, ids/names/groups from s3df-status-api/resources.yaml.
S3DF_RESOURCES: dict[str, ResourceMeta] = {
    meta.id: meta
    for meta in (
        ResourceMeta(
            id="ada",
            name="Batch (ada)",
            description="S3DF Slurm batch partition for Ada GPU nodes.",
            group="compute",
            resource_type=status_models.ResourceType.compute,
            supported_endpoints=(status_models.Endpoint.compute,),
        ),
        ResourceMeta(
            id="ampere",
            name="Batch (ampere)",
            description="S3DF Slurm batch partition for Ampere GPU nodes.",
            group="compute",
            resource_type=status_models.ResourceType.compute,
            supported_endpoints=(status_models.Endpoint.compute,),
        ),
        ResourceMeta(
            id="turing",
            name="Batch (turing)",
            description="S3DF Slurm batch partition for Turing GPU nodes.",
            group="compute",
            resource_type=status_models.ResourceType.compute,
            supported_endpoints=(status_models.Endpoint.compute,),
        ),
        ResourceMeta(
            id="milano",
            name="Batch (milano)",
            description="S3DF Slurm batch partition for Milano CPU nodes.",
            group="compute",
            resource_type=status_models.ResourceType.compute,
            supported_endpoints=(status_models.Endpoint.compute,),
        ),
        ResourceMeta(
            id="torino",
            name="Batch (torino)",
            description="S3DF Slurm batch partition for Torino CPU nodes.",
            group="compute",
            resource_type=status_models.ResourceType.compute,
            supported_endpoints=(status_models.Endpoint.compute,),
        ),
        ResourceMeta(
            id="roma",
            name="Batch (roma)",
            description="S3DF Slurm batch partition for Roma CPU nodes.",
            group="compute",
            resource_type=status_models.ResourceType.compute,
            supported_endpoints=(status_models.Endpoint.compute,),
        ),
        ResourceMeta(
            id="hopper",
            name="Batch (hopper)",
            description="S3DF Slurm batch partition for Hopper GPU nodes.",
            group="compute",
            resource_type=status_models.ResourceType.compute,
            supported_endpoints=(status_models.Endpoint.compute,),
        ),
        ResourceMeta(
            id="sdfhome",
            name="Storage (sdfhome)",
            description="S3DF Weka cluster for home directories (/sdf/home).",
            group="storage",
            resource_type=status_models.ResourceType.storage,
            supported_endpoints=(status_models.Endpoint.filesystem,),
        ),
        ResourceMeta(
            id="sdfdata",
            name="Storage (sdfdata)",
            description="S3DF Weka cluster for project/group data (sdfdata).",
            group="storage",
            resource_type=status_models.ResourceType.storage,
            supported_endpoints=(status_models.Endpoint.filesystem,),
        ),
        ResourceMeta(
            id="sdfk8s",
            name="Storage (sdfk8s)",
            description="S3DF Weka cluster for Kubernetes persistent volumes (sdfk8s).",
            group="storage",
            resource_type=status_models.ResourceType.storage,
        ),
        ResourceMeta(
            id="sdfscratch",
            name="Storage (sdfscratch)",
            description="S3DF Weka cluster for scratch storage (/sdf/scratch).",
            group="storage",
            resource_type=status_models.ResourceType.storage,
            supported_endpoints=(status_models.Endpoint.filesystem,),
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
