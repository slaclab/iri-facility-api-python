"""Owner/resource scoping and include_spec for live (slurmctld) job lookups."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.routers.compute import models as compute_models
from app.s3df import compute_adapter
from app.s3df.compute_adapter import SLACComputeAdapter


TEST_USER = SimpleNamespace(id="amithm", unix_username="amithm")
TEST_UID = 47886
OTHER_UID = 12345
MILANO = SimpleNamespace(id="milano")


def _limit(number=None, *, infinite=False):
    return SimpleNamespace(set=number is not None or infinite, infinite=infinite, number=number or 0)


def _live_job(job_id, *, uid=TEST_UID, user_name="", partition="milano", time_limit=_limit(2)):
    """Duck-typed slurmrestd v0041 job info.

    Prod slurmrestd cannot resolve user names, so user_name is "" and only
    the numeric user_id identifies the owner.
    """
    return SimpleNamespace(
        job_id=job_id,
        job_state=["RUNNING"],
        user_id=uid,
        user_name=user_name,
        partition=partition,
        account="scs:default@milano",
        name="iri-test",
        command="",
        current_working_directory="/sdf/home/a/amithm",
        standard_output="/sdf/home/a/amithm/out.txt",
        node_count=_limit(1),
        time_limit=time_limit,
    )


@pytest.fixture(autouse=True)
def caller_uid(monkeypatch):
    headers = {"x-auth-request-uid": str(TEST_UID)}
    monkeypatch.setattr(compute_adapter, "get_auth_headers", lambda: headers)
    return headers


def _adapter(monkeypatch, *, jobs=None, job=None) -> SLACComputeAdapter:
    adapter = SLACComputeAdapter()
    api = MagicMock()
    api.slurm_v0041_get_jobs = MagicMock(return_value=SimpleNamespace(jobs=jobs or []))
    api.slurm_v0041_get_job = MagicMock(return_value=SimpleNamespace(jobs=[job] if job else []))
    monkeypatch.setattr(adapter, "_get_slurm_context", lambda user: (api, {}))
    return adapter


@pytest.mark.asyncio
async def test_get_jobs_returns_only_callers_jobs_on_the_resource(monkeypatch):
    adapter = _adapter(monkeypatch, jobs=[
        _live_job(1, uid=OTHER_UID),
        _live_job(2),
        _live_job(3, partition="torino"),
        _live_job(4, partition="roma,milano"),
        _live_job(5, uid=OTHER_UID, partition="torino"),
    ])

    jobs = await adapter.get_jobs(resource=MILANO, user=TEST_USER)

    assert [j["id"] for j in jobs] == ["2", "4"]


@pytest.mark.asyncio
async def test_get_jobs_paginates_after_filtering(monkeypatch):
    adapter = _adapter(monkeypatch, jobs=[
        _live_job(1, uid=OTHER_UID),
        _live_job(2),
        _live_job(3, uid=OTHER_UID),
        _live_job(4),
        _live_job(5),
    ])

    jobs = await adapter.get_jobs(resource=MILANO, user=TEST_USER, offset=1, limit=1)

    assert [j["id"] for j in jobs] == ["4"]


@pytest.mark.asyncio
async def test_get_job_hides_other_users_live_job(monkeypatch):
    adapter = _adapter(monkeypatch, job=_live_job(7, uid=OTHER_UID))

    with pytest.raises(HTTPException) as exc_info:
        await adapter.get_job(resource=MILANO, user=TEST_USER, job_id="7", include_spec=True)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_jobs_are_hidden_without_caller_identity(monkeypatch, caller_uid):
    caller_uid.clear()
    adapter = _adapter(monkeypatch, jobs=[_live_job(2)], job=_live_job(2))

    assert await adapter.get_jobs(resource=MILANO, user=TEST_USER) == []
    with pytest.raises(HTTPException) as exc_info:
        await adapter.get_job(resource=MILANO, user=TEST_USER, job_id="2")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_user_name_is_used_when_slurm_reports_it(monkeypatch, caller_uid):
    caller_uid.clear()
    adapter = _adapter(monkeypatch, jobs=[
        _live_job(1, uid=None, user_name="amithm"),
        _live_job(2, uid=None, user_name="someone-else"),
    ])

    jobs = await adapter.get_jobs(resource=MILANO, user=TEST_USER)

    assert [j["id"] for j in jobs] == ["1"]


@pytest.mark.asyncio
async def test_include_spec_survives_job_model_serialization(monkeypatch):
    adapter = _adapter(monkeypatch, job=_live_job(8))

    job = await adapter.get_job(resource=MILANO, user=TEST_USER, job_id="8", include_spec=True)
    body = compute_models.Job.model_validate(job).model_dump(exclude_unset=True)

    spec = body["job_spec"]
    assert spec["attributes"] == {"queue_name": "milano", "account": "scs:default@milano", "duration": 120}
    assert spec["resources"] == {"node_count": 1}
    assert spec["directory"] == "/sdf/home/a/amithm"
    assert spec["stdout_path"] == "/sdf/home/a/amithm/out.txt"
    assert spec["executable"] is None  # slurm reports "" for jobs submitted with an inline script


@pytest.mark.asyncio
@pytest.mark.parametrize("time_limit", [None, _limit(), _limit(infinite=True)])
async def test_unset_or_unlimited_time_limit_is_valid_spec(monkeypatch, time_limit):
    adapter = _adapter(monkeypatch, job=_live_job(9, time_limit=time_limit))

    job = await adapter.get_job(resource=MILANO, user=TEST_USER, job_id="9", include_spec=True)

    assert compute_models.Job.model_validate(job).job_spec.attributes.duration is None
