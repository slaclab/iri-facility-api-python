
"""
Integration test for SLACComputeAdapter against a live slurmrestd.

Exercises the adapter's submit_job, get_job, get_jobs, and cancel_job
methods directly — no reimplementation of slurmrestd_client logic.

Usage:
  SLURM_JWT_KEY_PATH=/path/to/jwt_hs256.key \
    python -m app.s3df.slurm.test_slurmrestd --user amithm

  With partition/account:
    python -m app.s3df.slurm.test_slurmrestd --user amithm --partition roma --account myacct

  List jobs only (no submit/cancel):
    python -m app.s3df.slurm.test_slurmrestd --user amithm --skip-submit

  Flood test (submit N jobs concurrently, measure latency):
    python -m app.s3df.slurm.test_slurmrestd --user amithm --flood 50

  Flood with job arrays (each of the N submissions is a --array-size element array):
    python -m app.s3df.slurm.test_slurmrestd --user amithm --flood 10 --array-size 20

  Flood + concurrency limit (at most M in-flight at once):
    python -m app.s3df.slurm.test_slurmrestd --user amithm --flood 100 --concurrency 10
"""

import argparse
import asyncio
import logging
import statistics
import sys
import time

from app.routers.compute.models import JobSpec, JobAttributes, ResourceSpec
from app.s3df.compute_adapter import SLACComputeAdapter

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)


async def run_tests(args):
    adapter = SLACComputeAdapter()

    # Build user object via the adapter's own get_user
    user = await adapter.get_user(args.user, "test-key", None)
    resource = None  # adapter does not use the resource object

    # ── get_jobs ────────────────────────────────────────────────────────
    log.info("── get_jobs ──")
    try:
        jobs = await adapter.get_jobs(resource, user, offset=0, limit=10)
        print(jobs)
        log.info("Jobs for %s: %d returned", args.user, len(jobs))
        for j in jobs:
            log.info("  job_id=%s  state=%s", j["id"], j["status"]["state"])
    except Exception as e:
        log.error("get_jobs failed: %s", e)
        sys.exit(1)

    if args.skip_submit:
        log.info("--skip-submit set, done.")
        return

    # ── submit_job ──────────────────────────────────────────────────────
    log.info("── submit_job ──")
    job_spec = JobSpec(
        name="iri-integration-test",
        executable="#!/bin/bash\n\nhostname\nsleep 15\n",
        attributes=JobAttributes(
            duration=60,
            queue_name=args.partition,
            account=args.account,
        ),
        resources=ResourceSpec(node_count=1),
        directory=f"/sdf/home/{args.user[0]}/{args.user}",
        environment={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    try:
        result = await adapter.submit_job(resource, user, job_spec)
        job_id = result["id"]
        log.info("Submitted job_id=%s  state=%s", job_id, result["status"]["state"])
    except Exception as e:
        log.error("submit_job failed: %s", e)
        sys.exit(1)

    # ── get_job ─────────────────────────────────────────────────────────
    log.info("── get_job(%s) ──", job_id)
    time.sleep(1)
    try:
        job = await adapter.get_job(resource, user, job_id)
        log.info("  state=%s", job["status"]["state"])
    except Exception as e:
        log.error("get_job failed: %s", e)

    # ── cancel_job ──────────────────────────────────────────────────────
    log.info("── cancel_job(%s) ──", job_id)
    # try:
    #     cancelled = await adapter.cancel_job(resource, user, job_id)
    #     log.info("  cancelled=%s", cancelled)
    # except Exception as e:
    #     log.error("cancel_job failed: %s", e)

    # ── verify cancel ───────────────────────────────────────────────────
    time.sleep(1)
    # try:
    #     job = await adapter.get_job(resource, user, job_id)
    #     log.info("  post-cancel state=%s", job["status"]["state"])
    # except Exception as e:
    #     log.error("post-cancel get_job failed: %s", e)

    log.info("── done ──")


# ---------------------------------------------------------------------------
# Flood / load test
# ---------------------------------------------------------------------------

async def run_flood(args):
    """
    Submit --flood jobs (optionally as job arrays) through SLACComputeAdapter,
    measure submit latency, then bulk-query get_jobs to stress slurmrestd.
    Useful for gauging CPU/memory impact on the slurmctld VM.
    """
    adapter = SLACComputeAdapter()
    user = await adapter.get_user(args.user, "test-key", None)
    resource = None

    n_jobs = args.flood
    concurrency = args.concurrency or n_jobs
    sem = asyncio.Semaphore(concurrency)

    def _make_spec(idx: int) -> JobSpec:
        attrs = JobAttributes(
            duration=60,
            queue_name=args.partition,
            account=args.account,
        )
        if args.array_size:
            attrs.custom_attributes = {"array": f"0-{args.array_size - 1}"}
        return JobSpec(
            name=f"iri-flood-{idx:04d}",
            executable="#!/bin/bash\nhostname\nsleep 5\n",
            attributes=attrs,
            resources=ResourceSpec(node_count=1),
            directory=f"/sdf/home/{args.user[0]}/{args.user}",
            environment={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        )

    # -- submit phase -------------------------------------------------------
    submit_latencies: list[float] = []
    submitted_ids: list[str] = []
    errors = 0

    async def _submit_one(idx: int):
        nonlocal errors
        async with sem:
            spec = _make_spec(idx)
            t0 = time.monotonic()
            try:
                result = await adapter.submit_job(resource, user, spec)
                elapsed = time.monotonic() - t0
                submit_latencies.append(elapsed)
                submitted_ids.append(result["id"])
            except Exception as e:
                errors += 1
                log.error("submit %d failed: %s", idx, e)

    log.info("── flood: submitting %d jobs (concurrency=%d, array_size=%s) ──",
             n_jobs, concurrency, args.array_size or "none")

    wall_start = time.monotonic()
    await asyncio.gather(*[_submit_one(i) for i in range(n_jobs)])
    wall_submit = time.monotonic() - wall_start

    # -- report submit stats ------------------------------------------------
    log.info("── submit results ──")
    log.info("  total jobs : %d  (errors: %d)", n_jobs, errors)
    log.info("  wall clock : %.2f s", wall_submit)
    if submit_latencies:
        log.info("  latency min/median/p95/max : %.3f / %.3f / %.3f / %.3f s",
                 min(submit_latencies),
                 statistics.median(submit_latencies),
                 sorted(submit_latencies)[int(len(submit_latencies) * 0.95)],
                 max(submit_latencies))
        log.info("  throughput : %.1f submits/s", len(submit_latencies) / wall_submit)

    # -- query phase --------------------------------------------------------
    log.info("── flood: querying get_jobs ──")
    t0 = time.monotonic()
    try:
        jobs = await adapter.get_jobs(resource, user, offset=0, limit=5000)
        query_time = time.monotonic() - t0
        log.info("  get_jobs returned %d jobs in %.3f s", len(jobs), query_time)
    except Exception as e:
        log.error("  get_jobs failed: %s", e)

    # -- per-job get_job spot-checks (sample up to 10) ----------------------
    sample = submitted_ids[:10]
    if sample:
        log.info("── flood: spot-checking %d individual get_job calls ──", len(sample))
        get_latencies: list[float] = []
        for jid in sample:
            t0 = time.monotonic()
            try:
                job = await adapter.get_job(resource, user, jid)
                get_latencies.append(time.monotonic() - t0)
                log.info("  job %s  state=%s", jid, job["status"]["state"])
            except Exception as e:
                log.error("  get_job(%s) failed: %s", jid, e)
        if get_latencies:
            log.info("  get_job latency min/max : %.3f / %.3f s",
                     min(get_latencies), max(get_latencies))

    log.info("── flood done ──")


def main():
    parser = argparse.ArgumentParser(description="Integration test for SLACComputeAdapter")
    parser.add_argument("--user", required=True, help="Unix username (becomes JWT sun claim)")
    parser.add_argument("--partition", default=None, help="Slurm partition for test job")
    parser.add_argument("--account", default=None, help="Slurm account for test job")
    parser.add_argument("--skip-submit", action="store_true", help="Only list jobs, skip submit/cancel")
    parser.add_argument("--flood", type=int, default=0, metavar="N",
                        help="Submit N jobs concurrently for load testing")
    parser.add_argument("--array-size", type=int, default=0, metavar="M",
                        help="Each flood job becomes a job array of size M")
    parser.add_argument("--concurrency", type=int, default=0, metavar="C",
                        help="Max in-flight submits (default: unlimited)")
    args = parser.parse_args()

    if args.flood:
        asyncio.run(run_flood(args))
    else:
        asyncio.run(run_tests(args))


if __name__ == "__main__":
    main()