"""
S3DF Task Adapter

Forwards IRI task operations to fs-facade-service:
  - put_task: submits the filesystem operation to fs-facade without polling,
    records the IRI task id → fs-facade task id and owner, and returns immediately.
  - get_task: proxies GET /task/{fs_task_id} on fs-facade and translates the
    result into the IRI Task model.
  - delete_task: removes the record and deletes from fs-facade.

Every read and delete is scoped to the task's owner; other users' task ids
behave as if they do not exist. Records expire after settings.fs_task_ttl.

The class-level _tasks map ensures all IriRouter adapter instances (filesystem
router's task_adapter and the task router's own adapter) share the same state.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from time import monotonic

from fastapi import HTTPException

from app.request_context import get_auth_headers
from app.routers.status import models as status_models
from app.routers.task import facility_adapter as task_adapter
from app.routers.task import models as task_models
from app.s3df.auth.authenticated_adapter import S3DFAuthenticatedAdapter
from app.s3df.clients import FsFacadeError, get_fs_facade_client
from app.s3df.config import settings
from app.types.user import User

LOG = logging.getLogger(__name__)
_REQUIRED_FS_AUTH_HEADERS = (
    "x-auth-request-uid",
    "x-auth-request-primary-gid",
)


def _model_dict(val) -> dict:
    """Return a non-None-valued dict from a Pydantic model or plain dict."""
    if hasattr(val, "model_dump"):
        return {k: v for k, v in val.model_dump().items() if v is not None}
    return {k: v for k, v in val.items() if v is not None}


def _strip_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _required_fs_auth_headers() -> dict[str, str]:
    auth = get_auth_headers()
    missing = [name for name in _REQUIRED_FS_AUTH_HEADERS if not auth.get(name)]
    if missing:
        raise FsFacadeError(
            f"Missing required filesystem identity headers: {', '.join(missing)}",
            status_code=401,
        )

    try:
        int(auth["x-auth-request-uid"])
        primary_gid = int(auth["x-auth-request-primary-gid"])
        supplemental_gids = [
            int(gid.strip())
            for gid in auth.get("x-auth-request-gids", "").split(",")
            if gid.strip()
        ]
    except ValueError as exc:
        raise FsFacadeError(
            "Invalid filesystem identity headers",
            status_code=400,
        ) from exc

    normalized = dict(auth)
    normalized["x-auth-request-gids"] = ",".join(
        str(gid)
        for gid in dict.fromkeys([primary_gid, *supplemental_gids])
    )
    return normalized


def _required_fs_auth_headers_or_http_error() -> dict[str, str]:
    try:
        return _required_fs_auth_headers()
    except FsFacadeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@dataclass
class _TaskRecord:
    fs_task_id: str
    owner: str
    command: task_models.TaskCommand
    created_at: float


async def _submit_to_fs_facade(task: task_models.TaskCommand) -> str:
    """Map an IRI TaskCommand to the matching fs-facade HTTP call and return the fs task_id."""
    auth = _required_fs_auth_headers()
    client = get_fs_facade_client()
    cmd, args = task.command, task.args

    # GET endpoints — params only
    if cmd == "file":
        return await client.submit("GET", "/filesystem/file",
            params={"path": args["path"]}, headers=auth)
    if cmd == "stat":
        return await client.submit("GET", "/filesystem/stat",
            params=_strip_none({"path": args["path"], "dereference": args.get("dereference")}),
            headers=auth)
    if cmd == "ls":
        return await client.submit("GET", "/filesystem/ls",
            params=_strip_none({"path": args["path"], "show_hidden": args.get("show_hidden")}),
            headers=auth)
    if cmd == "head":
        return await client.submit("GET", "/filesystem/head",
            params=_strip_none({"path": args["path"], "lines": args.get("lines"), "bytes": args.get("file_bytes")}),
            headers=auth)
    if cmd == "tail":
        return await client.submit("GET", "/filesystem/tail",
            params=_strip_none({"path": args["path"], "lines": args.get("lines"), "bytes": args.get("file_bytes")}),
            headers=auth)
    if cmd == "view":
        return await client.submit("GET", "/filesystem/view",
            params=_strip_none({"path": args["path"], "size": args.get("size"), "offset": args.get("offset")}),
            headers=auth)
    if cmd == "checksum":
        return await client.submit("GET", "/filesystem/checksum",
            params={"path": args["path"]}, headers=auth)
    if cmd == "download":
        return await client.submit("GET", "/filesystem/download",
            params={"path": args["path"]}, headers=auth)

    # DELETE endpoint
    if cmd == "rm":
        return await client.submit("DELETE", "/filesystem/rm",
            params={"path": args["path"]}, headers=auth)

    # POST endpoints — JSON body
    if cmd == "mkdir":
        return await client.submit("POST", "/filesystem/mkdir",
            json_body=_model_dict(args["request_model"]), headers=auth)
    if cmd == "symlink":
        return await client.submit("POST", "/filesystem/symlink",
            json_body=_model_dict(args["request_model"]), headers=auth)
    if cmd == "compress":
        return await client.submit("POST", "/filesystem/compress",
            json_body=_model_dict(args["request_model"]), headers=auth)
    if cmd == "extract":
        return await client.submit("POST", "/filesystem/extract",
            json_body=_model_dict(args["request_model"]), headers=auth)
    if cmd == "mv":
        return await client.submit("POST", "/filesystem/mv",
            json_body=_model_dict(args["request_model"]), headers=auth)
    if cmd == "cp":
        return await client.submit("POST", "/filesystem/cp",
            json_body=_model_dict(args["request_model"]), headers=auth)
    if cmd == "upload":
        return await client.submit("POST", "/filesystem/upload",
            json_body={"path": args["path"], "content": args["content"]}, headers=auth)
    
    # PUT endpoints — JSON body
    if cmd == "chmod":
        return await client.submit("PUT", "/filesystem/chmod",
            json_body=_model_dict(args["request_model"]), headers=auth)
    if cmd == "chown":
        return await client.submit("PUT", "/filesystem/chown",
            json_body=_model_dict(args["request_model"]), headers=auth)

    raise ValueError(f"Unknown filesystem command: {cmd}")


class S3DFTaskAdapter(S3DFAuthenticatedAdapter, task_adapter.FacilityAdapter):
    """Task adapter that proxies operations to fs-facade-service."""

    # Class-level map shared across all IriRouter-created instances.
    _tasks: dict[str, _TaskRecord] = {}  # iri_task_id → record

    def __init__(self):
        pass

    @staticmethod
    def _owned_record(user: User, task_id: str) -> _TaskRecord | None:
        record = S3DFTaskAdapter._tasks.get(task_id)
        if record is None or record.owner != user.id:
            return None
        return record

    @staticmethod
    def _prune_expired() -> None:
        # Only forgets IRI's record. fs-facade expires its own copy: it only
        # accepts DELETE /task from the owner, and this may run in another
        # user's request.
        cutoff = monotonic() - settings.fs_task_ttl
        for iri_id, record in list(S3DFTaskAdapter._tasks.items()):
            if record.created_at < cutoff:
                del S3DFTaskAdapter._tasks[iri_id]

    async def get_user(self, user_id: str, api_key: str, client_ip: str | None, globus_introspect: dict | None = None):
        class _User:
            def __init__(self, uid: str, key: str):
                self.id = uid
                self.unix_username = uid
                self.api_key = key

        return _User(user_id, api_key)

    async def put_task(
        self,
        user: User,
        resource: status_models.Resource,
        task: task_models.TaskCommand,
    ) -> task_models.TaskSubmitResponse:
        try:
            fs_task_id = await _submit_to_fs_facade(task)
        except FsFacadeError as exc:
            LOG.error("fs-facade submit failed for %s:%s: %s", task.router, task.command, exc)
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        S3DFTaskAdapter._prune_expired()
        iri_task_id = str(uuid.uuid4())
        S3DFTaskAdapter._tasks[iri_task_id] = _TaskRecord(
            fs_task_id=fs_task_id,
            owner=user.id,
            command=task,
            created_at=monotonic(),
        )
        LOG.info("submitted task %s (fs: %s)  %s:%s", iri_task_id, fs_task_id, task.router, task.command)
        return task_models.TaskSubmitResponse(task_id=iri_task_id)

    async def get_task(self, user: User, task_id: str) -> task_models.Task | None:
        record = self._owned_record(user, task_id)
        if record is None:
            return None
        auth = _required_fs_auth_headers_or_http_error()
        try:
            fs_task = await get_fs_facade_client().get_task(record.fs_task_id, headers=auth)
        except FsFacadeError as exc:
            LOG.warning("fs-facade get_task failed for %s: %s", record.fs_task_id, exc)
            return None

        raw_result = fs_task.get("result")
        # fs-facade workers emit JSON strings shaped like the IRI response models
        # (e.g. {"output": ...}). Parse to a dict so Task.result deserialises
        # cleanly on the IRI side; fall back to {"output": <str>} for plain strings.
        if isinstance(raw_result, str):
            try:
                raw_result = json.loads(raw_result)
            except (ValueError, TypeError):
                raw_result = {"output": raw_result}

        # Wrap lists — Task.result expects dict | None
        if isinstance(raw_result, list):
            raw_result = {"output": raw_result}

        return task_models.Task(
            id=task_id,
            status=task_models.TaskStatus(fs_task["status"]),
            result=raw_result,
            command=record.command,
        )

    async def get_tasks(self, user: User) -> list[task_models.Task]:
        S3DFTaskAdapter._prune_expired()
        tasks = []
        for iri_id, record in list(S3DFTaskAdapter._tasks.items()):
            if record.owner != user.id:
                continue
            t = await self.get_task(user, iri_id)
            if t is not None:
                tasks.append(t)
        return tasks

    async def delete_task(self, user: User, task_id: str) -> None:
        record = self._owned_record(user, task_id)
        if record is None:
            return
        auth = _required_fs_auth_headers_or_http_error()
        S3DFTaskAdapter._tasks.pop(task_id, None)
        try:
            client = get_fs_facade_client()
            await client._get_client().delete(f"/task/{record.fs_task_id}", headers=auth)
        except Exception as exc:
            LOG.debug("fs-facade delete_task %s ignored: %s", record.fs_task_id, exc)
