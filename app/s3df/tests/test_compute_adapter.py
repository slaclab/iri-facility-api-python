"""Tests for the S3DF compute adapter's historical (slurmdbd) job lookup."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.routers.compute.models import JobState
from app.s3df.compute_adapter import SLACComputeAdapter
from slurmrestd_client.exceptions import ApiException


TEST_USER = SimpleNamespace(id="amithm", unix_username="amithm")


def _db_record(*, job_id=12345, state="COMPLETED", user="amithm", return_code=0):
    """Build a minimal slurmdbd accounting record (duck-typed)."""
    return SimpleNamespace(
        job_id=job_id,
        name="my-job",
        state=SimpleNamespace(current=[state]) if state is not None else None,
        user=user,
        partition="milano",
        account="proj123",
        allocation_nodes=2,
        exit_code=SimpleNamespace(
            return_code=SimpleNamespace(set=True, number=return_code)
        ),
        time=SimpleNamespace(limit=SimpleNamespace(set=True, number=60)),
    )


def _make_adapter(monkeypatch, *, live_side_effect, db_records):
    """Wire an adapter whose live lookup and slurmdbd lookup are mocked."""
    adapter = SLACComputeAdapter()

    live_api = MagicMock()
    live_api.slurm_v0041_get_job = MagicMock(side_effect=live_side_effect)
    monkeypatch.setattr(adapter, "_get_slurm_context", lambda user: (live_api, {}))

    db_api = MagicMock()
    if isinstance(db_records, Exception):
        db_api.slurmdb_v0041_get_job = MagicMock(side_effect=db_records)
    else:
        db_api.slurmdb_v0041_get_job = MagicMock(
            return_value=SimpleNamespace(jobs=db_records)
        )
    monkeypatch.setattr(
        adapter, "_get_slurmdb_context", lambda user: (db_api, {}, user.unix_username)
    )
    return adapter, live_api, db_api


def _not_live(*_a, **_k):
    """Simulate the live scheduler no longer knowing about the job."""
    raise ApiException(status=404)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slurm_state,expected",
    [
        ("COMPLETED", JobState.COMPLETED),
        ("FAILED", JobState.FAILED),
        ("CANCELLED", JobState.CANCELED),
        ("TIMEOUT", JobState.FAILED),
    ],
)
async def test_historical_returns_final_state(monkeypatch, slurm_state, expected):
    adapter, _, db_api = _make_adapter(
        monkeypatch,
        live_side_effect=_not_live,
        db_records=[_db_record(state=slurm_state)],
    )
    job = await adapter.get_job(resource=None, user=TEST_USER, job_id="12345", historical=True)
    assert job["id"] == "12345"
    assert job["status"]["state"] == expected
    # Queried by job_id alone — the lightest slurmdbd query.
    db_api.slurmdb_v0041_get_job.assert_called_once()
    assert db_api.slurmdb_v0041_get_job.call_args.args[0] == "12345"


@pytest.mark.asyncio
async def test_historical_picks_most_recent_record(monkeypatch):
    adapter, _, _ = _make_adapter(
        monkeypatch,
        live_side_effect=_not_live,
        db_records=[_db_record(state="FAILED"), _db_record(state="COMPLETED")],
    )
    job = await adapter.get_job(resource=None, user=TEST_USER, job_id="12345", historical=True)
    assert job["status"]["state"] == JobState.COMPLETED


@pytest.mark.asyncio
async def test_historical_include_spec(monkeypatch):
    adapter, _, _ = _make_adapter(
        monkeypatch,
        live_side_effect=_not_live,
        db_records=[_db_record()],
    )
    job = await adapter.get_job(
        resource=None, user=TEST_USER, job_id="12345", historical=True, include_spec=True
    )
    assert job["job_spec"]["attributes"]["queue_name"] == "milano"
    assert job["job_spec"]["attributes"]["duration"] == 3600  # 60 min -> secs
    assert job["job_spec"]["resources"]["node_count"] == 2


@pytest.mark.asyncio
async def test_historical_empty_records_is_404(monkeypatch):
    adapter, _, _ = _make_adapter(
        monkeypatch, live_side_effect=_not_live, db_records=[]
    )
    with pytest.raises(HTTPException) as exc:
        await adapter.get_job(resource=None, user=TEST_USER, job_id="12345", historical=True)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_historical_other_users_job_is_404(monkeypatch):
    adapter, _, _ = _make_adapter(
        monkeypatch,
        live_side_effect=_not_live,
        db_records=[_db_record(user="someone_else")],
    )
    with pytest.raises(HTTPException) as exc:
        await adapter.get_job(resource=None, user=TEST_USER, job_id="12345", historical=True)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_historical_slurmdbd_404_is_404(monkeypatch):
    adapter, _, _ = _make_adapter(
        monkeypatch, live_side_effect=_not_live, db_records=ApiException(status=404)
    )
    with pytest.raises(HTTPException) as exc:
        await adapter.get_job(resource=None, user=TEST_USER, job_id="12345", historical=True)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_historical_slurmdbd_error_is_500(monkeypatch):
    adapter, _, _ = _make_adapter(
        monkeypatch, live_side_effect=_not_live, db_records=ApiException(status=503)
    )
    with pytest.raises(HTTPException) as exc:
        await adapter.get_job(resource=None, user=TEST_USER, job_id="12345", historical=True)
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_non_historical_missing_job_does_not_hit_slurmdbd(monkeypatch):
    adapter, _, db_api = _make_adapter(
        monkeypatch, live_side_effect=_not_live, db_records=[_db_record()]
    )
    with pytest.raises(HTTPException) as exc:
        await adapter.get_job(resource=None, user=TEST_USER, job_id="12345", historical=False)
    assert exc.value.status_code == 404
    db_api.slurmdb_v0041_get_job.assert_not_called()
