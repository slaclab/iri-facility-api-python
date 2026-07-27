"""Tests for static S3DF storage discovery."""

import datetime

import pytest
from fastapi import HTTPException

from app.routers.status import models as status_models
from app.routers.storage import models as storage_models
from app.s3df.storage_adapter import S3DFStorageAdapter
from app.types.user import User


def _resource(
    resource_id: str = "sdfhome",
    resource_type=status_models.ResourceType.storage,
) -> status_models.Resource:
    return status_models.Resource(
        id=resource_id,
        name=resource_id,
        description=resource_id,
        site_id="s3df",
        group="storage",
        resource_type=resource_type,
        current_status=status_models.Status.up,
        last_modified=datetime.datetime(2026, 7, 22, tzinfo=datetime.timezone.utc),
    )


def _user(username: str = "amithm") -> User:
    return User(
        id=username,
        name=username,
        api_key="test-token",
        client_ip="127.0.0.1",
    )


@pytest.mark.asyncio
async def test_sdfhome_returns_static_user_home():
    locations = await S3DFStorageAdapter().get_locations(
        _resource(), _user(), None, None, None, None
    )

    assert len(locations) == 1
    location = locations[0]
    assert location.logical_name is storage_models.LogicalName.home
    assert location.path == "/sdf/home/a/amithm"
    assert location.filesystem == "sdfhome"
    assert location.shared is False
    assert location.access == storage_models.AccessPermissions(
        read=True,
        write=True,
        execute=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent",
    [
        storage_models.StorageIntent.read,
        storage_models.StorageIntent.write,
        storage_models.StorageIntent.staging,
    ],
)
async def test_sdfhome_supports_non_archive_intents(intent):
    locations = await S3DFStorageAdapter().get_locations(
        _resource(), _user(), storage_models.LogicalName.home, None, None, intent
    )
    assert [location.path for location in locations] == ["/sdf/home/a/amithm"]


@pytest.mark.asyncio
async def test_sdfhome_filters_unsupported_logical_name_and_archive_intent():
    adapter = S3DFStorageAdapter()

    scratch = await adapter.get_locations(
        _resource(), _user(), storage_models.LogicalName.scratch, None, None, None
    )
    archive = await adapter.get_locations(
        _resource(),
        _user(),
        None,
        None,
        None,
        storage_models.StorageIntent.long_term_storage,
    )

    assert scratch == []
    assert archive == []


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_id", ["sdfdata", "sdfscratch", "sdfk8s", "ada"])
async def test_other_resources_are_not_implemented(resource_id):
    resource_type = (
        status_models.ResourceType.compute
        if resource_id == "ada"
        else status_models.ResourceType.storage
    )

    with pytest.raises(HTTPException) as exc_info:
        await S3DFStorageAdapter().get_locations(
            _resource(resource_id, resource_type), _user(), None, None, None, None
        )

    assert exc_info.value.status_code == 501


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project", "allocation"),
    [("project-1", None), (None, "allocation-1")],
)
async def test_project_and_allocation_discovery_are_not_implemented(project, allocation):
    with pytest.raises(HTTPException) as exc_info:
        await S3DFStorageAdapter().get_locations(
            _resource(), _user(), None, project, allocation, None
        )

    assert exc_info.value.status_code == 501


@pytest.mark.asyncio
async def test_sdfhome_access_endpoint_registry_is_empty():
    endpoints = await S3DFStorageAdapter().get_access_endpoints(
        _resource(), _user(), None, None
    )
    assert endpoints == []


@pytest.mark.asyncio
async def test_other_resource_access_endpoints_are_not_implemented():
    with pytest.raises(HTTPException) as exc_info:
        await S3DFStorageAdapter().get_access_endpoints(
            _resource("sdfdata"), _user(), None, None
        )
    assert exc_info.value.status_code == 501


@pytest.mark.asyncio
async def test_invalid_username_cannot_be_used_as_a_path_component():
    with pytest.raises(HTTPException) as exc_info:
        await S3DFStorageAdapter().get_locations(
            _resource(), _user("../other-user"), None, None, None, None
        )
    assert exc_info.value.status_code == 500