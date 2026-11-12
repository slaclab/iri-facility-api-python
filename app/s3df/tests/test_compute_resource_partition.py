"""The /compute/{resource_id} path picks the Slurm partition.

Regression: a resource missing from the status registry (blackwell) resolved
to ``None``, so submit_job silently fell back to ``SLURM_DEFAULT_PARTITION``
(ampere in prod) and get_jobs matched every partition.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.routers.compute import models as compute_models
from app.routers.status import models as status_models
from app.s3df import compute_adapter
from app.s3df import status_adapter as status_adapter_module
from app.s3df.compute_adapter import SLACComputeAdapter
from app.s3df.status_adapter import S3DFStatusAdapter


TEST_USER = SimpleNamespace(id="amithm", unix_username="amithm")
TEST_UID = 47886


@pytest.fixture(autouse=True)
def caller_uid(monkeypatch):
    headers = {"x-auth-request-uid": str(TEST_UID)}
    monkeypatch.setattr(compute_adapter, "get_auth_headers", lambda: headers)
    monkeypatch.setenv("SLURM_DEFAULT_PARTITION", "ampere")
    monkeypatch.setenv("SLURM_DEFAULT_ACCOUNT", "scs:default")


async def _resource(monkeypatch, resource_id):
    """Resolve resource_id exactly as the compute router does in prod."""
    client = AsyncMock()
    client.get_resource_status = AsyncMock(return_value={"resource_id": resource_id, "status": "up"})
    monkeypatch.setattr(status_adapter_module, "get_s3df_status_api_client", lambda: client)
    return await S3DFStatusAdapter().get_resource(resource_id)


def _adapter(monkeypatch, *, jobs=None):
    adapter = SLACComputeAdapter()
    api = MagicMock()
    api.slurm_v0041_post_job_submit = MagicMock(return_value=SimpleNamespace(job_id=101))
    api.slurm_v0041_get_jobs = MagicMock(return_value=SimpleNamespace(jobs=jobs or []))
    monkeypatch.setattr(adapter, "_get_slurm_context", lambda user: (api, {}))
    return adapter, api


def _submitted_partition(api) -> str:
    req = api.slurm_v0041_post_job_submit.call_args.kwargs["slurm_v0041_post_job_submit_request"]
    return req.job.partition


def _spec(**attributes) -> compute_models.JobSpec:
    return compute_models.JobSpec(
        executable="/bin/hostname",
        attributes=compute_models.JobAttributes(**attributes) if attributes else None,
    )


def _live_job(job_id, partition):
    return SimpleNamespace(
        job_id=job_id, job_state=["RUNNING"], user_id=TEST_UID, user_name="",
        partition=partition, account="scs:default", name="iri-test", command="",
        current_working_directory="/sdf/home/a/amithm", standard_output="",
        node_count=SimpleNamespace(set=True, infinite=False, number=1),
        time_limit=SimpleNamespace(set=True, infinite=False, number=2),
    )


@pytest.mark.asyncio
async def test_blackwell_is_a_known_compute_resource(monkeypatch):
    resource = await _resource(monkeypatch, "blackwell")

    assert resource is not None
    assert resource.resource_type is status_models.ResourceType.compute
    assert resource.group == "compute"


@pytest.mark.asyncio
async def test_submit_uses_path_resource_as_partition(monkeypatch):
    adapter, api = _adapter(monkeypatch)
    resource = await _resource(monkeypatch, "blackwell")

    job = await adapter.submit_job(resource=resource, user=TEST_USER, job_spec=_spec())

    assert _submitted_partition(api) == "blackwell"
    compute_models.Job.model_validate(job)


@pytest.mark.asyncio
async def test_submit_queue_name_overrides_path_resource(monkeypatch):
    adapter, api = _adapter(monkeypatch)
    resource = await _resource(monkeypatch, "milano")

    await adapter.submit_job(resource=resource, user=TEST_USER, job_spec=_spec(queue_name="milano,roma"))

    assert _submitted_partition(api) == "milano,roma"


@pytest.mark.asyncio
async def test_submit_to_unknown_resource_is_404_not_default_partition(monkeypatch):
    adapter, api = _adapter(monkeypatch)
    resource = await _resource(monkeypatch, "no-such-partition")
    assert resource is None

    with pytest.raises(HTTPException) as exc:
        await adapter.submit_job(resource=resource, user=TEST_USER, job_spec=_spec())

    assert exc.value.status_code == 404
    api.slurm_v0041_post_job_submit.assert_not_called()


@pytest.mark.asyncio
async def test_submit_to_storage_resource_is_404(monkeypatch):
    adapter, api = _adapter(monkeypatch)
    resource = await _resource(monkeypatch, "sdfhome")

    with pytest.raises(HTTPException) as exc:
        await adapter.submit_job(resource=resource, user=TEST_USER, job_spec=_spec())

    assert exc.value.status_code == 404
    api.slurm_v0041_post_job_submit.assert_not_called()


@pytest.mark.asyncio
async def test_get_jobs_on_blackwell_lists_only_blackwell_jobs(monkeypatch):
    adapter, _ = _adapter(monkeypatch, jobs=[
        _live_job(1, "blackwell"),
        _live_job(2, "ampere"),
        _live_job(3, "hopper,blackwell"),
    ])
    resource = await _resource(monkeypatch, "blackwell")

    jobs = await adapter.get_jobs(resource=resource, user=TEST_USER)

    assert [j["id"] for j in jobs] == ["1", "3"]


@pytest.mark.asyncio
async def test_get_jobs_on_unknown_resource_is_404_without_listing(monkeypatch):
    adapter, api = _adapter(monkeypatch, jobs=[_live_job(1, "ampere")])

    with pytest.raises(HTTPException) as exc:
        await adapter.get_jobs(resource=None, user=TEST_USER)

    assert exc.value.status_code == 404
    api.slurm_v0041_get_jobs.assert_not_called()
