"""Tests for the static S3DF status registry."""

from app.routers.status import models as status_models
from app.s3df.status_registry import S3DF_RESOURCES, parse_status, site_id


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


def test_registry_has_all_expected_resources():
    assert list(S3DF_RESOURCES.keys()) == EXPECTED_IDS


def test_registry_resource_types_match_yaml():
    by_id = S3DF_RESOURCES
    assert by_id["s3df-ssh-bastions"].resource_type is status_models.ResourceType.service
    assert by_id["s3df-interactive-nodes"].resource_type is status_models.ResourceType.compute
    assert by_id["s3df-docs"].resource_type is status_models.ResourceType.website
    assert by_id["s3df-storage"].resource_type is status_models.ResourceType.storage
    assert by_id["s3df-kubernetes"].resource_type is status_models.ResourceType.system
    assert by_id["s3df-dtns"].resource_type is status_models.ResourceType.network


def test_registry_groups_match_yaml():
    by_id = S3DF_RESOURCES
    assert by_id["s3df-ssh-bastions"].group == "access"
    assert by_id["s3df-batch-servers"].group == "compute"
    assert by_id["s3df-coact"].group == "accounts"
    assert by_id["s3df-storage"].group == "storage"
    assert by_id["s3df-dtns"].group == "data-transfer"


def test_parse_status_round_trip():
    assert parse_status("up") is status_models.Status.up
    assert parse_status("down") is status_models.Status.down
    assert parse_status("degraded") is status_models.Status.degraded
    assert parse_status("unknown") is status_models.Status.unknown
    assert parse_status(None) is None
    assert parse_status("garbage") is status_models.Status.unknown


def test_site_id_is_non_empty():
    assert site_id()
