"""Tests for the S3DF account adapter's allocation mapping (coact-backed)."""

import pytest
from fastapi import HTTPException

from app.routers.account import models as account_models
from app.s3df.account_adapter import S3DFAccountAdapter
from app.types.scalars import AllocationUnit
from app.types.user import User


def _user(name: str = "alice") -> User:
    return User(id=name, name=name, api_key="test", client_ip="127.0.0.1")


def _project() -> account_models.Project:
    return account_models.Project(
        id="repo-1",
        name="lcls:mfx101592326",
        description="",
        user_ids=["alice", "bob"],
    )


def _compute_alloc(alloc_id: str, cluster: str, allocated: float = 1000, used: float = 250) -> dict:
    # Shaped like coact's currentComputeAllocations: the id field is "Id", not "_id".
    return {
        "Id": alloc_id,
        "repoid": "repo-1",
        "clustername": cluster,
        "allocated": allocated,
        "usage": [{"resourceHours": used}],
    }


class FakeCoact:
    def __init__(self, compute_allocs=None, user_allocs=None):
        self.compute_allocs = compute_allocs
        self.user_allocs = user_allocs or []

    async def get_repo_compute_allocations(self, repo_id):
        return self.compute_allocs

    async def get_user_allocation(self, repo_id, allocation_id):
        return self.user_allocs


@pytest.mark.asyncio
async def test_project_allocations_map_coact_ids_and_clusters():
    adapter = S3DFAccountAdapter(coact_client=FakeCoact(compute_allocs=[
        _compute_alloc("alloc-roma", "roma"),
        _compute_alloc("alloc-ada", "ada", allocated=40, used=0),
    ]))

    allocations = await adapter.get_project_allocations(project=_project(), user=_user())

    assert [(a.id, a.capability_id) for a in allocations] == [
        ("alloc-roma", "roma"),
        ("alloc-ada", "ada"),
    ]
    assert allocations[0].entries[0].allocation == 1000
    assert allocations[0].entries[0].usage == 250
    assert allocations[0].capability_uri.endswith("/account/capabilities/roma")


@pytest.mark.asyncio
@pytest.mark.parametrize("compute_allocs", [None, []])
async def test_project_without_allocations_is_404(compute_allocs):
    adapter = S3DFAccountAdapter(coact_client=FakeCoact(compute_allocs=compute_allocs))

    with pytest.raises(HTTPException) as exc_info:
        await adapter.get_project_allocations(project=_project(), user=_user())

    assert exc_info.value.status_code == 404


def _project_allocation() -> account_models.ProjectAllocation:
    return account_models.ProjectAllocation(
        id="alloc-roma",
        project_id="repo-1",
        capability_id="roma",
        entries=[account_models.AllocationEntry(allocation=1000, usage=200, unit=AllocationUnit.node_hours)],
    )


@pytest.mark.asyncio
async def test_user_allocation_applies_callers_percent():
    adapter = S3DFAccountAdapter(coact_client=FakeCoact(
        compute_allocs=[_compute_alloc("alloc-roma", "roma")],
        user_allocs=[{"username": "bob", "percent": 10}, {"username": "alice", "percent": 50}],
    ))

    [user_alloc] = await adapter.get_user_allocations(user=_user("alice"), project_allocation=_project_allocation())

    assert user_alloc.user_id == "alice"
    assert user_alloc.entries[0].allocation == 500
    assert user_alloc.entries[0].usage == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("user_allocs", [[], [{"username": "bob", "percent": 10}]])
async def test_user_without_explicit_share_gets_full_allocation(user_allocs):
    adapter = S3DFAccountAdapter(coact_client=FakeCoact(
        compute_allocs=[_compute_alloc("alloc-roma", "roma")],
        user_allocs=user_allocs,
    ))

    [user_alloc] = await adapter.get_user_allocations(user=_user("alice"), project_allocation=_project_allocation())

    assert user_alloc.entries[0].allocation == 1000


@pytest.mark.asyncio
async def test_user_allocations_empty_when_project_has_no_compute_allocations():
    adapter = S3DFAccountAdapter(coact_client=FakeCoact(compute_allocs=None))

    assert await adapter.get_user_allocations(user=_user(), project_allocation=_project_allocation()) == []
