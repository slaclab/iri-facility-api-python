"""S3DF idempotency store, exercised through upstream's run_with_idempotency."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from slurmrestd_client.exceptions import ApiException

from app.idempotency import build_body_hash, build_cache_key, create_store, run_with_idempotency
from app.routers.compute import models as compute_models
from app.s3df.compute_adapter import SLACComputeAdapter
from app.s3df.idempotency import InMemoryIdempotencyStore


TEST_USER = SimpleNamespace(id="amithm", unix_username="amithm")
MILANO = SimpleNamespace(id="milano")
KEY = build_cache_key("amithm", "7b0f8a5e-0000-4000-8000-000000000001", "submit_job")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _job_spec(account: str = "scs:default") -> compute_models.JobSpec:
    return compute_models.JobSpec(
        executable="/bin/hostname",
        attributes=compute_models.JobAttributes(account=account, queue_name="milano", duration=60),
    )


def _slurm_adapter(monkeypatch, job_ids=(4242,)):
    adapter = SLACComputeAdapter()
    api = MagicMock()
    api.slurm_v0041_post_job_submit = MagicMock(side_effect=[SimpleNamespace(job_id=j) for j in job_ids])
    monkeypatch.setattr(adapter, "_get_slurm_context", lambda user: (api, {}))
    return adapter, api


def _submit(store, adapter, job_spec):
    return run_with_idempotency(
        store,
        KEY,
        build_body_hash(job_spec.model_dump()),
        lambda: adapter.submit_job(resource=MILANO, user=TEST_USER, job_spec=job_spec),
    )


def test_store_is_loadable_from_env(monkeypatch):
    monkeypatch.setenv("IRI_IDEMPOTENCY_STORE", "app.s3df.idempotency.InMemoryIdempotencyStore")
    assert isinstance(create_store(), InMemoryIdempotencyStore)


@pytest.mark.asyncio
async def test_retry_replays_the_first_submission_without_resubmitting(monkeypatch):
    store = InMemoryIdempotencyStore()
    adapter, api = _slurm_adapter(monkeypatch, job_ids=(4242, 4243))

    first = await _submit(store, adapter, _job_spec())
    retry = await _submit(store, adapter, _job_spec())

    assert api.slurm_v0041_post_job_submit.call_count == 1
    assert first.headers["Idempotency-Key-Reply"] == "miss"
    assert retry.headers["Idempotency-Key-Reply"] == "hit"
    assert json.loads(retry.body) == json.loads(first.body)
    assert json.loads(first.body)["id"] == "4242"
    assert json.loads(first.body)["status"]["state"] == "queued"


@pytest.mark.asyncio
async def test_same_key_with_a_different_body_is_rejected(monkeypatch):
    store = InMemoryIdempotencyStore()
    adapter, api = _slurm_adapter(monkeypatch)
    await _submit(store, adapter, _job_spec())

    with pytest.raises(HTTPException) as exc_info:
        await _submit(store, adapter, _job_spec(account="other:project"))

    assert exc_info.value.status_code == 422
    assert api.slurm_v0041_post_job_submit.call_count == 1


@pytest.mark.asyncio
async def test_in_flight_duplicate_conflicts():
    store = InMemoryIdempotencyStore()
    assert await store.check_and_lock(KEY, "body") == ("proceed", None, None)

    assert await store.check_and_lock(KEY, "body") == ("conflict", None, None)


@pytest.mark.asyncio
async def test_failed_submission_releases_the_key(monkeypatch):
    store = InMemoryIdempotencyStore()
    adapter, api = _slurm_adapter(monkeypatch, job_ids=(4242,))
    api.slurm_v0041_post_job_submit.side_effect = [ApiException(status=500), SimpleNamespace(job_id=4243)]

    with pytest.raises(HTTPException) as exc_info:
        await _submit(store, adapter, _job_spec())
    retry = await _submit(store, adapter, _job_spec())

    assert exc_info.value.status_code == 500
    assert json.loads(retry.body)["id"] == "4243"


@pytest.mark.asyncio
async def test_expired_entries_are_dropped():
    clock = Clock()
    store = InMemoryIdempotencyStore(ttl=100, lock_ttl=10, clock=clock)
    await store.check_and_lock(KEY, "body")
    await store.store_result(KEY, "body", {"id": "1"}, 200)

    clock.now += 99
    assert (await store.check_and_lock(KEY, "body"))[0] == "hit"
    clock.now += 2
    assert (await store.check_and_lock(KEY, "body"))[0] == "proceed"


@pytest.mark.asyncio
async def test_stale_lock_expires_so_a_crashed_request_can_be_retried():
    clock = Clock()
    store = InMemoryIdempotencyStore(ttl=100, lock_ttl=10, clock=clock)
    await store.check_and_lock(KEY, "body")

    clock.now += 11

    assert (await store.check_and_lock(KEY, "body"))[0] == "proceed"


@pytest.mark.asyncio
async def test_sweep_bounds_memory_to_live_keys():
    clock = Clock()
    store = InMemoryIdempotencyStore(ttl=100, lock_ttl=10, clock=clock)
    for i in range(50):
        await store.check_and_lock(f"key-{i}", "body")
        await store.store_result(f"key-{i}", "body", {"id": str(i)}, 200)

    clock.now += 200
    await store.check_and_lock("fresh", "body")

    assert list(store._entries) == ["fresh"]
