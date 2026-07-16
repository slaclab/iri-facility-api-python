"""
SLAC S3DF Slurm Compute Adapter for IRI Facility API

Auth model: IRI acts as JWT broker.
- IRI holds the shared jwt_hs256.key (same key mounted on slurmrestd).
- On every request IRI mints a short-lived HS256 JWT with `sun` = unix username.
- That JWT is forwarded as X-SLURM-USER-TOKEN to slurmrestd.
- slurmrestd validates it locally — no external auth call.

Required env vars:
  SLURM_REST_URL        e.g. http://slurmrestd:6820
  SLURM_JWT_KEY_PATH    path to jwt_hs256.key file (binary, 32 bytes)
  SLURM_JWT_LIFETIME    JWT lifetime in seconds (default: 3600)
"""

import os
import time
import logging
import base64
from typing import Optional
from ..routers.compute import facility_adapter as compute_adapter
from ..routers.compute.models import JobState
from app.s3df.auth.authenticated_adapter import S3DFAuthenticatedAdapter
import jwt  # PyJWT
from slurmrestd_client.api_client import ApiClient
from slurmrestd_client.api.slurm_api import SlurmApi
from slurmrestd_client.api.slurmdb_api import SlurmdbApi
from slurmrestd_client.configuration import Configuration
from slurmrestd_client.exceptions import ApiException
from slurmrestd_client.models.slurm_v0041_post_job_submit_request import (
    SlurmV0041PostJobSubmitRequest,
)
from slurmrestd_client.models.slurm_v0041_post_job_submit_request_job import (
    SlurmV0041PostJobSubmitRequestJob,
)
from slurmrestd_client.models.slurm_v0041_post_job_submit_request_jobs_inner_time_limit import (
    SlurmV0041PostJobSubmitRequestJobsInnerTimeLimit,
)
from slurmrestd_client.models.slurm_v0041_post_job_submit_request_jobs_inner_memory_per_cpu import (
    SlurmV0041PostJobSubmitRequestJobsInnerMemoryPerCpu,
)
from fastapi import HTTPException
from pydantic import ConfigDict, ValidationError

from ..routers.compute import models as compute_models
from ..types.user import User
from ..routers.status import models as status_models
from ..request_context import get_iri_facility_project
import shlex

logger = logging.getLogger(__name__)


class SlurmV0041PostJobSubmitRequestJobStrict(SlurmV0041PostJobSubmitRequestJob):
    # we reject unexpected fields to enable raising ValidationError
    # TODO: we could see if the autogeneration could be configured to make
    # this strict by default
    model_config = ConfigDict(
        populate_by_name=True,
        validate_assignment=True,
        protected_namespaces=(),
        extra="forbid", 
    )

# ---------------------------------------------------------------------------
# Slurm → IRI state mapping
# ---------------------------------------------------------------------------
# Import JobState from wherever IRI defines it — adjust path as needed.
# from app.routers.compute.models import JobState, Job, JobStatus, JobSpec

SLURM_TO_IRI_STATE: dict[str, JobState] = {
    "PENDING":       JobState.QUEUED,
    "CONFIGURING":   JobState.NEW,
    "RUNNING":       JobState.ACTIVE,
    "COMPLETED":     JobState.COMPLETED,
    "CANCELLED":     JobState.CANCELED,
    "FAILED":        JobState.FAILED,
    "TIMEOUT":       JobState.FAILED,
    "PREEMPTED":     JobState.FAILED,
    "NODE_FAIL":     JobState.FAILED,
    "SUSPENDED":     JobState.CANCELED,
    "COMPLETING":    JobState.ACTIVE,
    "STAGE_OUT":     JobState.ACTIVE,
    "BOOT_FAIL":     JobState.FAILED,
    "DEADLINE":      JobState.FAILED,
    "OUT_OF_MEMORY": JobState.FAILED,
    "RESIZING":      JobState.ACTIVE,
    "REQUEUED":      JobState.QUEUED,
    "REVOKED":       JobState.FAILED,
    "SIGNALING":     JobState.ACTIVE,
    "SPECIAL_EXIT":  JobState.FAILED,
    "STOPPED":       JobState.CANCELED,
}

# Map from Slurm partition name → GPU type string for GRES
PARTITION_GPU_TYPE: dict[str, str] = {
    "ampere": "a100",
    "turing": "geforce_rtx_2080_ti",
    "ada":    "l40s",
    "hopper": "h200",
}


# ---------------------------------------------------------------------------
# JWT minting — IRI signs tokens using the shared key
# ---------------------------------------------------------------------------

def _load_jwt_key() -> bytes:
    """Load the HS256 shared key from environment variable."""
    key_str = os.environ.get("slurm_jwt")
    
    return base64.b64decode(key_str)

def _mint_slurm_jwt(unix_username: str) -> str:
    """
    Mint a short-lived HS256 JWT accepted by slurmrestd.

    Claims required by Slurm (https://slurm.schedmd.com/jwt.html):
      sun  — Slurm user name (unix username)
      exp  — expiry (unix timestamp)
      iat  — issued at
    """
    lifetime = int(os.environ.get("SLURM_JWT_LIFETIME", 3600))
    now = int(time.time())
    payload = {
        "sun": unix_username, #slurm rest user -> authenticated via jwt
        "exp": now + lifetime,
    }
    key = _load_jwt_key()
    token = jwt.encode(payload, key, algorithm="HS256")
    # PyJWT ≥2.0 returns str; older versions return bytes
    return token if isinstance(token, str) else token.decode()


# ---------------------------------------------------------------------------
# slurmrestd_client helpers
# ---------------------------------------------------------------------------

def _build_api_client(token: str) -> ApiClient:
    url = os.environ.get("SLURM_REST_URL")
    if not url:
        raise RuntimeError("SLURM_REST_URL environment variable is not set")
    cfg = Configuration(host=url)
    cfg.verify_ssl = False #os.environ.get("SLURM_VERIFY_SSL", "true").lower() == "true"
    # api_key on the config is not used for header auth — we pass headers manually
    return ApiClient(cfg)


def _build_slurm_api(token: str) -> SlurmApi:
    return SlurmApi(_build_api_client(token))


def _build_slurmdb_api(token: str) -> SlurmdbApi:
    """Client for the Slurm accounting DB (slurmdbd) endpoints."""
    return SlurmdbApi(_build_api_client(token))


def _auth_headers(unix_username: str, token: str) -> dict:
    return {
        "X-SLURM-USER-NAME": unix_username,
        "X-SLURM-USER-TOKEN": token,
    }


def _primary_state(job_state) -> str:
    """Extract the primary state string from whatever slurmrestd returns."""
    if isinstance(job_state, list):
        return job_state[0].upper() if job_state else "UNKNOWN"
    if isinstance(job_state, str):
        return job_state.upper()
    if hasattr(job_state, "value"):          # enum wrapper
        return str(job_state.value).upper()
    return str(job_state).upper()


def _map_state(slurm_state) -> JobState:
    primary = _primary_state(slurm_state)
    mapped = SLURM_TO_IRI_STATE.get(primary)
    if not mapped:
        logger.warning("Unknown Slurm state %r — mapping to FAILED", primary)
        return JobState.FAILED
    return mapped


def _job_from_slurm_info(job_info, include_spec: bool = False) -> dict:
    """
    Convert a slurmrestd job info object to a plain dict that IRI can
    deserialise into its Job model.  Adjust field names to match your
    exact IRI Job / JobStatus / JobSpec models.
    """
    state_str = _map_state(job_info.job_state)
    job_dict = {
        "id": str(job_info.job_id),
        "status": {
            "state": state_str,
        },
    }
    if include_spec:
        tl = getattr(job_info, "time_limit", None)
        if tl is None:
            duration_secs = 0
        elif isinstance(tl, (int, float)):
            duration_secs = int(tl) * 60
        elif getattr(tl, "set", False):
            duration_secs = int(getattr(tl, "number", 0) or 0) * 60
        else:
            duration_secs = 0

        job_dict["job_spec"] = {
            "name": getattr(job_info, "name", None),
            "executable": getattr(job_info, "batch_script", None),
            "resources": {
                "node_count": getattr(job_info, "num_nodes", None),
            },
            "attributes": {
                "queue_name": getattr(job_info, "partition", None),
                "account": getattr(job_info, "account", None),
                "duration": duration_secs,
            },
        }
    return job_dict

def _job_from_slurmdb_info(job_record, include_spec: bool = False) -> dict:
    """
    Convert a slurmdbd accounting record (sacct-backed) to a plain dict that
    IRI can deserialise into its Job model.

    The accounting record has a different shape than the live-scheduler job
    object handled by `_job_from_slurm_info`:
      - the final state lives under `state.current` (a list of strings)
      - the numeric id is `job_id`, the wall-clock limit is under `time.limit`
    The returned dict shape is kept identical to `_job_from_slurm_info` so the
    router's `models.Job` deserialisation is unchanged.
    """
    state_obj = getattr(job_record, "state", None)
    current = getattr(state_obj, "current", None) if state_obj is not None else None
    state_str = _map_state(current)

    status: dict = {"state": state_str}
    exit_code = getattr(job_record, "exit_code", None)
    return_code = getattr(exit_code, "return_code", None) if exit_code is not None else None
    if return_code is not None and getattr(return_code, "set", False):
        status["exit_code"] = getattr(return_code, "number", None)

    job_dict = {
        "id": str(job_record.job_id),
        "status": status,
    }

    if include_spec:
        time_obj = getattr(job_record, "time", None)
        tl = getattr(time_obj, "limit", None) if time_obj is not None else None
        if tl is not None and getattr(tl, "set", False):
            duration_secs = int(getattr(tl, "number", 0) or 0) * 60
        else:
            duration_secs = 0

        job_dict["job_spec"] = {
            "name": getattr(job_record, "name", None),
            "resources": {
                "node_count": getattr(job_record, "allocation_nodes", None),
            },
            "attributes": {
                "queue_name": getattr(job_record, "partition", None),
                "account": getattr(job_record, "account", None),
                "duration": duration_secs,
            },
        }
    return job_dict


def _build_batch_script(job_spec: compute_models.JobSpec) -> str:
    """
    Build a Slurm batch-script body from an IRI JobSpec.

    slurmrestd's `script` field requires an inline shell program, not a bare
    path. IRI's `executable` is a path/command, so we wrap it here. This lets
    callers pass `executable="/path/to/run.sh"` (plus `arguments`) without
    embedding a shebang or the script's contents.
    """
    executable = job_spec.executable
    if not executable:
        raise HTTPException(
            status_code=422,
            detail="job_spec.executable is required for Slurm submission",
        )

    # Back-compat: caller already supplied a full script (starts with shebang).
    if executable.startswith("#!"):
        return executable

    lines = ["#!/bin/bash"]
    if job_spec.pre_launch:
        lines.append(job_spec.pre_launch)

    cmd = []
    if job_spec.launcher:
        cmd.append(job_spec.launcher)
    cmd.append(shlex.quote(executable))
    cmd.extend(shlex.quote(str(a)) for a in job_spec.arguments)
    lines.append(" ".join(cmd))

    if job_spec.post_launch:
        lines.append(job_spec.post_launch)

    return "\n".join(lines) + "\n"

# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class SLACComputeAdapter(S3DFAuthenticatedAdapter, compute_adapter.FacilityAdapter):
    """
    IRI FacilityAdapter backed by SLAC S3DF slurmrestd.

    Implements every method called by the IRI compute router:
      submit_job, submit_job_script, update_job,
      get_job, get_jobs, cancel_job
    plus the user helper used by the router:
      get_user
    """

    # -- AuthenticatedAdapter methods ---------------------------------------

    async def get_user(self, user_id: str, api_key: str, client_ip: str | None, globus_introspect: dict | None = None):
        """
        Return a minimal user object.  The unix_username is the critical field —
        it becomes the `sun` claim in the Slurm JWT.

        In production, resolve user_id → unix username via your LDAP / IRIS lookup.
        For now we use user_id directly (adjust as needed).
        """
        class _User:
            def __init__(self, uid: str):
                self.id = uid                # IRI user id
                self.unix_username = uid     # maps to Slurm `sun` claim
                self.api_key = api_key

        return _User(user_id)

    # -- internal helpers ---------------------------------------------------

    def _get_slurm_context(self, user):
        """Return (api, headers) for the authenticated user."""
        unix_user = getattr(user, "unix_username", user.id)
        token = _mint_slurm_jwt(unix_user)
        api = _build_slurm_api(token)
        headers = _auth_headers(unix_user, token)
        return api, headers

    def _get_slurmdb_context(self, user):
        """Return (slurmdb_api, headers, unix_user) for the authenticated user.

        The per-user JWT (`sun` claim) scopes accounting results to this user via
        Slurm accounting permissions, so no separate username filter is required.
        """
        unix_user = getattr(user, "unix_username", user.id)
        token = _mint_slurm_jwt(unix_user)
        api = _build_slurmdb_api(token)
        headers = _auth_headers(unix_user, token)
        return api, headers, unix_user
    
    
    # -- submit_job ---------------------------------------------------------

    async def submit_job(
        self,
        resource: status_models.Resource,
        user: User,
        job_spec: compute_models.JobSpec,
    ) -> compute_models.Job:
        """
        POST /compute/job/{resource_id}
        Maps IRI JobSpec → SlurmV0041PostJobSubmitRequest and submits.
        """
        api, headers = self._get_slurm_context(user)

        # --- resource fields with safe defaults ---
        node_count = 1
        tasks = None
        tasks_per_node = None
        cpus_per_task = None
        tres_per_task = None
        exclusive = ["true"]
        memory_per_node = None
        duration_mins = 60
        partition = None
        account = None
        reservation = None
        environment = ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]

        name = job_spec.name
        executable = job_spec.executable
        argv = job_spec.arguments or None
        cwd = str(job_spec.directory) if job_spec.directory else None
        stdin = job_spec.stdin_path
        stdout = job_spec.stdout_path
        stderr = job_spec.stderr_path

        if job_spec.environment:
            environment = [f"{k}={v}" for k, v in job_spec.environment.items()]

        if job_spec.attributes:
            if job_spec.attributes.duration is not None:
                duration_mins = max(1, int(job_spec.attributes.duration // 60))
            partition = job_spec.attributes.queue_name
            account = job_spec.attributes.account
            reservation = job_spec.attributes.reservation_id

        partition = partition or os.environ.get("SLURM_DEFAULT_PARTITION")
        account = account or get_iri_facility_project() or os.environ.get("SLURM_DEFAULT_ACCOUNT")

        if job_spec.resources:
            node_count = job_spec.resources.node_count or 1
            tasks = job_spec.resources.process_count
            tasks_per_node = job_spec.resources.processes_per_node
            cpus_per_task = job_spec.resources.cpu_cores_per_process
            if job_spec.resources.gpu_cores_per_process:
                gpu_type = PARTITION_GPU_TYPE.get(partition or "")
                if gpu_type:
                    tres_per_task = f"gres/gpu:{gpu_type}:{job_spec.resources.gpu_cores_per_process}"
                else:
                    tres_per_task = f"gres/gpu:{job_spec.resources.gpu_cores_per_process}"
            if not job_spec.resources.exclusive_node_use:
                exclusive = ["false"]
            if job_spec.resources.memory:
                memory_mb = max(1, job_spec.resources.memory // (1024 * 1024))
                memory_per_node = SlurmV0041PostJobSubmitRequestJobsInnerMemoryPerCpu(set=True, number=memory_mb)

        custom_attributes = job_spec.attributes.custom_attributes if job_spec.attributes else {}

        try:
            slurm_job = SlurmV0041PostJobSubmitRequestJobStrict(
                nodes=str(node_count),
                tasks=tasks,
                tasks_per_node=tasks_per_node,
                cpus_per_task=cpus_per_task,
                tres_per_task=tres_per_task,
                exclusive=exclusive,
                memory_per_node=memory_per_node,
                time_limit=SlurmV0041PostJobSubmitRequestJobsInnerTimeLimit(set=True, number=duration_mins),
                name=name,
                script=_build_batch_script(job_spec),
                argv=argv,
                partition=partition,
                account=account,
                reservation=reservation,
                environment=environment,
                current_working_directory=cwd,
                standard_input=stdin,
                standard_output=stdout,
                standard_error=stderr,
                **custom_attributes
            )
        except (ValidationError, TypeError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid job submission parameters: {exc}",
            ) from exc

        req = SlurmV0041PostJobSubmitRequest(job=slurm_job)

        try:
            resp = api.slurm_v0041_post_job_submit(
                slurm_v0041_post_job_submit_request=req,
                _headers=headers,
            )
            logger.info("Job submitted: job_id=%s", resp.job_id)
            return compute_models.Job(
                id=str(resp.job_id),
                # TODO: check if 200 always mean it is queued
                status=compute_models.JobStatus(state=JobState.QUEUED),
            )
        except ApiException as exc:
            logger.error("submit_job failed: %s", exc)
            raise HTTPException(status_code=500, detail="Slurm submission failed") from exc

    # -- submit_job_script --------------------------------------------------

    async def submit_job_script(
        self, resource, user, job_script_path: str, args: list = []
    ) -> dict:
        """
        POST /compute/job/script/{resource_id}

        job_script_path is a path on the COMPUTE NODE filesystem.
        We build a minimal wrapper script that executes it.
        slurmrestd requires the script to be inlined in the request body.
        """
        if args:
            arg_str = " ".join(str(a) for a in args)
            script = f"#!/bin/bash\n{job_script_path} {arg_str}\n"
        else:
            script = f"#!/bin/bash\n{job_script_path}\n"

        unix_user = getattr(user, "unix_username", user.id)

        # Reuse submit_job with a minimal spec carrying just the script
        class _MinimalSpec:
            executable = script
            name = os.path.basename(job_script_path)
            directory = f"/sdf/home/{unix_user[0]}/{unix_user}"
            stdout_path = None
            stderr_path = None
            environment = None
            resources = None
            attributes = None

        return await self.submit_job(resource, user, _MinimalSpec())

    # -- update_job ---------------------------------------------------------

    async def update_job(self, resource, user, job_spec, job_id: str) -> compute_models.Job:
        """
        PUT /compute/job/{resource_id}/{job_id}

        slurmrestd v0.0.41 exposes POST /slurm/v0.0.41/job/{job_id} for updates.
        Only a subset of fields can be changed after submission (time_limit,
        priority, partition, etc.).  Fields not supported by Slurm are ignored.
        """
        api, headers = self._get_slurm_context(user)

        update_fields: dict = {}

        if job_spec:
            attributes = getattr(job_spec, "attributes", None)
            if attributes:
                duration = getattr(attributes, "duration", None)
                if duration is not None:
                    total_secs = (
                        duration.total_seconds()
                        if hasattr(duration, "total_seconds")
                        else float(duration)
                    )
                    update_fields["time_limit"] = SlurmV0041PostJobSubmitRequestJobsInnerTimeLimit(
                        set=True, number=max(1, int(total_secs // 60))
                    )
                partition = getattr(attributes, "queue_name", None)
                if partition:
                    update_fields["partition"] = partition
                account = getattr(attributes, "account", None) or get_iri_facility_project()
                if account:
                    update_fields["account"] = account

        try:
            # v0041 job update endpoint
            api.slurm_v0041_post_job(job_id, update_fields, _headers=headers)
        except ApiException as exc:
            logger.error("update_job %s failed: %s", job_id, exc)
            raise HTTPException(status_code=500, detail=f"Slurm update failed for job {job_id}") from exc

        job = await self.get_job(resource, user, job_id)
        return compute_models.Job.model_validate(job)

    # -- get_job ------------------------------------------------------------

    async def get_job(
        self,
        resource,
        user,
        job_id: str,
        historical: bool = False,
        include_spec: bool = False,
    ) -> dict:
        """GET /compute/status/{resource_id}/{job_id}"""
        api, headers = self._get_slurm_context(user)

        try:
            # Try active jobs first
            resp = api.slurm_v0041_get_job(job_id, _headers=headers)
            if resp and resp.jobs:
                return _job_from_slurm_info(resp.jobs[0], include_spec)
        except ApiException as exc:
            if exc.status != 404:
                logger.exception("Slurm get_job failed for job %s", job_id)
                raise HTTPException(status_code=500, detail="Slurm get_job failed") from exc

        if historical:
            # The job is no longer live — fall back to slurmdbd (accounting DB)
            # for its final state. Query by job_id alone
            dbapi, db_headers, unix_user = self._get_slurmdb_context(user)
            try:
                db_resp = dbapi.slurmdb_v0041_get_job(str(job_id), _headers=db_headers)
            except ApiException as exc:
                if exc.status == 404:
                    raise HTTPException(status_code=404, detail=f"Job {job_id} not found") from exc
                logger.exception("slurmdbd get_job failed for job %s", job_id)
                raise HTTPException(status_code=500, detail="Slurm accounting lookup failed") from exc

            records = (db_resp.jobs if db_resp else None) or []
            # A job_id can map to multiple accounting records (job arrays /
            # duplicate submissions); the last is the most recent.
            record = records[-1] if records else None
            if record is None:
                raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

            # Defense in depth: the JWT already scopes results to this user, but
            # never surface another user's job even if that ever changes.
            record_user = getattr(record, "user", None)
            if record_user and record_user != unix_user:
                raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

            return _job_from_slurmdb_info(record, include_spec)

        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # -- get_jobs -----------------------------------------------------------

    async def get_jobs(
        self,
        resource,
        user,
        offset: int = 0,
        limit: int = 100,
        filters: Optional[dict] = None,
        historical: bool = False,
        include_spec: bool = False,
    ) -> list:
        """POST /compute/status/{resource_id}"""
        api, headers = self._get_slurm_context(user)

        if historical:
            # Historical *listing* would query slurmdbd's /jobs endpoint
            # (e.g. by users=), which scans a user's entire accounting history
            # and can be expensive. Only single-job historical lookup (get_job)
            # is supported for now; revisit listing once load is characterised.
            raise HTTPException(status_code=501, detail="Historical job listing is not implemented yet")

        try:
            resp = api.slurm_v0041_get_jobs(_headers=headers)
        except ApiException as exc:
            logger.exception("Slurm get_jobs failed")
            raise HTTPException(status_code=500, detail="Slurm get_jobs failed") from exc

        
        jobs = resp.jobs or []

        # Apply caller-supplied filters (key = Slurm job_info attribute name)
        if filters:
            for key, value in filters.items():
                jobs = [j for j in jobs if getattr(j, key, None) == value]

        # Pagination
        jobs = jobs[offset : offset + limit]

        return [_job_from_slurm_info(j, include_spec) for j in jobs]

    # -- cancel_job ---------------------------------------------------------

    async def cancel_job(self, resource, user, job_id: str) -> bool:
        """DELETE /compute/cancel/{resource_id}/{job_id}"""
        api, headers = self._get_slurm_context(user)

        try:
            api.slurm_v0041_delete_job(job_id, _headers=headers)
            logger.info("Cancelled job %s", job_id)
            return True
        except ApiException as exc:
            raise HTTPException(status_code=500, detail=f"Slurm cancel failed for job {job_id}") from exc