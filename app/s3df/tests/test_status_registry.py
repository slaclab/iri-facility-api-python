"""Tests for the static S3DF status registry."""

from app.routers.status import models as status_models
from app.s3df.status_registry import S3DF_RESOURCES, parse_status, site_id


EXPECTED_IDS = [
    "ada",
    "ampere",
    "turing",
    "milano",
    "torino",
    "roma",
    "hopper",
    "sdfhome",
    "sdfdata",
    "sdfk8s",
    "sdfscratch",
]


def test_registry_has_all_expected_resources():
    assert list(S3DF_RESOURCES.keys()) == EXPECTED_IDS


def test_registry_resource_types_match_yaml():
    by_id = S3DF_RESOURCES
    assert by_id["ada"].resource_type is status_models.ResourceType.compute
    assert by_id["sdfhome"].resource_type is status_models.ResourceType.storage


def test_registry_groups_match_yaml():
    by_id = S3DF_RESOURCES
    assert by_id["ada"].group == "compute"
    assert by_id["sdfhome"].group == "storage"


def test_parse_status_round_trip():
    assert parse_status("up") is status_models.Status.up
    assert parse_status("down") is status_models.Status.down
    assert parse_status("degraded") is status_models.Status.degraded
    assert parse_status("unknown") is status_models.Status.unknown
    assert parse_status(None) is None
    assert parse_status("garbage") is status_models.Status.unknown


def test_site_id_is_non_empty():
    assert site_id()
