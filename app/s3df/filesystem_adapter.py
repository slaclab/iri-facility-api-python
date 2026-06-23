"""
S3DF Filesystem Adapter

Forwards every IRI filesystem operation to the ``fs-facade-service``
microservice, polls the task endpoint until it reaches a terminal state,
and converts the result into the matching IRI response model.

Authentication: Dex JWT verification via S3DFAuthenticatedAdapter mixin.

See ``fs-facade-service/app/controllers/filesystem_controller.py`` for
the upstream wire format.
"""

import logging

from fastapi import HTTPException

from app.types.user import User
from app.s3df.auth.authenticated_adapter import S3DFAuthenticatedAdapter
from app.s3df.clients import (
    FsFacadeError,
    FsFacadeTimeout,
    get_fs_facade_client,
)
from app.routers.filesystem import facility_adapter, models
from app.routers.status import models as status_models
from app.request_context import get_auth_headers

LOG = logging.getLogger(__name__)


async def _fs_call(method: str, path: str, **kwargs):
    """Submit an op via fs-facade and translate transport/timeout errors to HTTP."""
    client = get_fs_facade_client()
    auth_headers = get_auth_headers()
    if auth_headers:
        LOG.debug("Forwarding auth headers to fs-facade: %s", list(auth_headers.keys()))
    try:
        return await client.call(method, path, headers=auth_headers or None, **kwargs)
    except FsFacadeTimeout as exc:
        LOG.warning(f"fs-facade timeout on {method} {path}: {exc}")
        raise HTTPException(status_code=504, detail=f"fs-facade timeout: {exc}") from exc
    except FsFacadeError as exc:
        LOG.error(f"fs-facade error on {method} {path}: {exc}")
        raise HTTPException(status_code=502, detail=f"fs-facade error: {exc}") from exc


def _file_model(payload) -> models.File:
    """Convert a fs-facade File JSON dict into the IRI File model."""
    if payload is None:
        raise HTTPException(status_code=502, detail="fs-facade returned no file payload")
    return models.File.model_validate(payload)


def _content_payload(text: str, *, content_type: models.ContentUnit, offset: int = 0) -> models.FileContent:
    """Wrap raw text from head/tail/view into the IRI FileContent shape."""
    text = text or ""
    return models.FileContent(
        content=text,
        content_type=content_type,
        start_position=offset,
        end_position=offset + len(text),
    )


class S3DFFilesystemAdapter(S3DFAuthenticatedAdapter, facility_adapter.FacilityAdapter):
    """Filesystem adapter that forwards operations to fs-facade-service."""

    async def get_user(self, user_id: str, api_key: str, client_ip: str | None, globus_introspect: dict | None = None):
        class _User:
            def __init__(self, uid: str, key: str):
                self.id = uid
                self.unix_username = uid
                self.api_key = key

        return _User(user_id, api_key)

    # --- Permissions / ownership ------------------------------------------

    async def chmod(self, resource: status_models.Resource, user: User, request_model: models.PutFileChmodRequest) -> models.PutFileChmodResponse:
        result = await _fs_call(
            "PUT", "/filesystem/chmod",
            json_body={"path": request_model.path, "mode": request_model.mode},
        )
        return models.PutFileChmodResponse(output=_file_model(result))

    async def chown(self, resource: status_models.Resource, user: User, request_model: models.PutFileChownRequest) -> models.PutFileChownResponse:
        result = await _fs_call(
            "PUT", "/filesystem/chown",
            json_body={
                "path": request_model.path,
                "owner": request_model.owner,
                "group": request_model.group,
            },
        )
        return models.PutFileChownResponse(output=_file_model(result))

    # --- Directory listing ------------------------------------------------

    async def ls(
        self,
        resource: status_models.Resource,
        user: User,
        path: str,
        show_hidden: bool,
        numeric_uid: bool,
        recursive: bool,
        dereference: bool,
    ) -> models.GetDirectoryLsResponse:
        # NOTE: fs-facade-service currently honours only `show_hidden`. The
        # other flags (numeric_uid, recursive, dereference) are silently
        # ignored. Track adding upstream support if/when needed.
        result = await _fs_call(
            "GET", "/filesystem/ls",
            params={"path": path, "show_hidden": str(show_hidden).lower()},
        )
        if not isinstance(result, list):
            raise HTTPException(status_code=502, detail=f"fs-facade ls returned {type(result).__name__}")
        return models.GetDirectoryLsResponse(output=[models.File.model_validate(f) for f in result])

    # --- File content reads -----------------------------------------------

    async def head(
        self,
        resource: status_models.Resource,
        user: User,
        path: str,
        file_bytes: int,
        lines: int,
        skip_trailing: bool,
    ) -> models.GetFileHeadResponse:
        params: dict = {"path": path}
        content_type = models.ContentUnit.lines
        if file_bytes is not None:
            params["bytes"] = file_bytes
            content_type = models.ContentUnit.bytes
        if lines is not None:
            params["lines"] = lines
        text = await _fs_call("GET", "/filesystem/head", params=params)
        return models.GetFileHeadResponse(output=_content_payload(text, content_type=content_type))

    async def tail(
        self,
        resource: status_models.Resource,
        user: User,
        path: str,
        file_bytes: int | None,
        lines: int | None,
        skip_heading: bool,
    ) -> models.GetFileTailResponse:
        params: dict = {"path": path}
        content_type = models.ContentUnit.lines
        if file_bytes is not None:
            params["bytes"] = file_bytes
            content_type = models.ContentUnit.bytes
        if lines is not None:
            params["lines"] = lines
        text = await _fs_call("GET", "/filesystem/tail", params=params)
        return models.GetFileTailResponse(output=_content_payload(text, content_type=content_type))

    async def view(
        self,
        resource: status_models.Resource,
        user: User,
        path: str,
        size: int,
        offset: int,
    ) -> models.GetViewFileResponse:
        text = await _fs_call(
            "GET", "/filesystem/view",
            params={"path": path, "size": size, "offset": offset},
        )
        return models.GetViewFileResponse(
            output=_content_payload(text, content_type=models.ContentUnit.bytes, offset=offset),
        )

    # --- Metadata ---------------------------------------------------------

    async def checksum(self, resource: status_models.Resource, user: User, path: str) -> models.GetFileChecksumResponse:
        result = await _fs_call("GET", "/filesystem/checksum", params={"path": path})
        return models.GetFileChecksumResponse(output=models.FileChecksum.model_validate(result))

    async def file(self, resource: status_models.Resource, user: User, path: str) -> models.GetFileTypeResponse:
        result = await _fs_call("GET", "/filesystem/file", params={"path": path})
        # `file` returns a bare string from the dispatcher.
        if isinstance(result, dict):
            result = result.get("output") or result.get("type") or ""
        return models.GetFileTypeResponse(output=str(result) if result is not None else None)

    async def stat(self, resource: status_models.Resource, user: User, path: str, dereference: bool) -> models.GetFileStatResponse:
        result = await _fs_call(
            "GET", "/filesystem/stat",
            params={"path": path, "dereference": str(dereference).lower()},
        )
        return models.GetFileStatResponse(output=models.FileStat.model_validate(result))

    # --- File management --------------------------------------------------

    async def rm(self, resource: status_models.Resource, user: User, path: str) -> models.RemoveResponse:
        await _fs_call("DELETE", "/filesystem/rm", params={"path": path})
        return models.RemoveResponse(output=f"Removed {path}")

    async def mkdir(self, resource: status_models.Resource, user: User, request_model: models.PostMakeDirRequest) -> models.PostMkdirResponse:
        result = await _fs_call(
            "POST", "/filesystem/mkdir",
            json_body={"path": request_model.path, "parent": request_model.parent},
        )
        return models.PostMkdirResponse(output=_file_model(result))

    async def symlink(
        self,
        resource: status_models.Resource,
        user: User,
        request_model: models.PostFileSymlinkRequest,
    ) -> models.PostFileSymlinkResponse:
        result = await _fs_call(
            "POST", "/filesystem/symlink",
            json_body={"path": request_model.path, "link_path": request_model.link_path},
        )
        return models.PostFileSymlinkResponse(output=_file_model(result))

    # --- Transfer ---------------------------------------------------------

    async def download(self, resource: status_models.Resource, user: User, path: str) -> models.GetFileDownloadResponse:
        result = await _fs_call("GET", "/filesystem/download", params={"path": path})
        # fs-facade returns the IRI-aligned shape: {"output": <base64>}.
        return models.GetFileDownloadResponse(**result) if isinstance(result, dict) else models.GetFileDownloadResponse(output=result)

    async def upload(self, resource: status_models.Resource, user: User, path: str, content: str) -> models.PutFileUploadResponse:
        result = await _fs_call(
            "POST", "/filesystem/upload",
            json_body={"path": path, "content": content},
        )
        # fs-facade returns the IRI-aligned shape: {"output": "<status message>"}.
        if isinstance(result, dict):
            return models.PutFileUploadResponse(**result)
        return models.PutFileUploadResponse(output=result if isinstance(result, str) else "File uploaded successfully")

    # --- Archive ----------------------------------------------------------

    async def compress(self, resource: status_models.Resource, user: User, request_model: models.PostCompressRequest) -> models.PostCompressResponse:
        # NOTE: fs-facade does not currently honour `match_pattern`; it is
        # silently dropped here.
        body = {
            "path": request_model.path,
            "target_path": request_model.target_path,
            "compression": request_model.compression.value if request_model.compression else "gzip",
            "dereference": request_model.dereference,
        }
        result = await _fs_call("POST", "/filesystem/compress", json_body=body)
        return models.PostCompressResponse(output=_file_model(result))

    async def extract(self, resource: status_models.Resource, user: User, request_model: models.PostExtractRequest) -> models.PostExtractResponse:
        body = {
            "path": request_model.path,
            "target_path": request_model.target_path,
            "compression": request_model.compression.value if request_model.compression else "gzip",
        }
        result = await _fs_call("POST", "/filesystem/extract", json_body=body)
        return models.PostExtractResponse(output=_file_model(result))

    async def mv(self, resource: status_models.Resource, user: User, request_model: models.PostMoveRequest) -> models.PostMoveResponse:
        result = await _fs_call(
            "POST", "/filesystem/mv",
            json_body={"path": request_model.path, "target_path": request_model.target_path},
        )
        return models.PostMoveResponse(output=_file_model(result))

    async def cp(self, resource: status_models.Resource, user: User, request_model: models.PostCopyRequest) -> models.PostCopyResponse:
        result = await _fs_call(
            "POST", "/filesystem/cp",
            json_body={
                "path": request_model.path,
                "target_path": request_model.target_path,
                "dereference": request_model.dereference,
            },
        )
        return models.PostCopyResponse(output=_file_model(result))
