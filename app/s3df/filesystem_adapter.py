"""
S3DF Filesystem Adapter

Thin proxy adapter for filesystem operations. Authenticates via Dex JWT,
enriches with POSIX identity from user-lookup, and will forward requests
to the filesystem microservice (same endpoints, same data model).
"""

import logging

from fastapi import HTTPException

from app.types.user import User
from app.s3df.auth.authenticated_adapter import S3DFAuthenticatedAdapter
from app.s3df.clients import get_user_lookup_client
from app.routers.filesystem import facility_adapter, models
from app.routers.status import models as status_models

LOG = logging.getLogger(__name__)


class S3DFFilesystemAdapter(S3DFAuthenticatedAdapter, facility_adapter.FacilityAdapter):
    """Filesystem adapter that enforces POSIX identity via user-lookup."""

    async def get_user(self, user_id: str, api_key: str, client_ip: str | None, globus_introspect: dict | None = None) -> User:
        try:
            lookup_data = await get_user_lookup_client().get_user(user_id)
        except ValueError:
            raise HTTPException(status_code=403, detail=f"User '{user_id}' not found in directory")
        except Exception as e:
            LOG.error(f"user-lookup service error: {e}")
            raise HTTPException(status_code=502, detail="User lookup service unavailable")

        return User(
            id=user_id,
            name=lookup_data.get("username", user_id),
            api_key=api_key,
            client_ip=client_ip,
        )

    # --- Filesystem operations (501 until microservice is connected) ---

    async def chmod(self, resource: status_models.Resource, user: User, request_model: models.PutFileChmodRequest) -> models.PutFileChmodResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def chown(self, resource: status_models.Resource, user: User, request_model: models.PutFileChownRequest) -> models.PutFileChownResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def ls(self, resource: status_models.Resource, user: User, path: str, show_hidden: bool, numeric_uid: bool, recursive: bool, dereference: bool) -> models.GetDirectoryLsResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def head(self, resource: status_models.Resource, user: User, path: str, file_bytes: int, lines: int, skip_trailing: bool) -> models.GetFileHeadResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def tail(self, resource: status_models.Resource, user: User, path: str, file_bytes: int | None, lines: int | None, skip_heading: bool) -> models.GetFileTailResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def view(self, resource: status_models.Resource, user: User, path: str, size: int, offset: int) -> models.GetViewFileResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def checksum(self, resource: status_models.Resource, user: User, path: str) -> models.GetFileChecksumResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def file(self, resource: status_models.Resource, user: User, path: str) -> models.GetFileTypeResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def stat(self, resource: status_models.Resource, user: User, path: str, dereference: bool) -> models.GetFileStatResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def rm(self, resource: status_models.Resource, user: User, path: str) -> models.RemoveResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def mkdir(self, resource: status_models.Resource, user: User, request_model: models.PostMakeDirRequest) -> models.PostMkdirResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def symlink(self, resource: status_models.Resource, user: User, request_model: models.PostFileSymlinkRequest) -> models.PostFileSymlinkResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def download(self, resource: status_models.Resource, user: User, path: str) -> models.GetFileDownloadResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def upload(self, resource: status_models.Resource, user: User, path: str, content: str) -> models.PutFileUploadResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def compress(self, resource: status_models.Resource, user: User, request_model: models.PostCompressRequest) -> models.PostCompressResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def extract(self, resource: status_models.Resource, user: User, request_model: models.PostExtractRequest) -> models.PostExtractResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def mv(self, resource: status_models.Resource, user: User, request_model: models.PostMoveRequest) -> models.PostMoveResponse:
        raise HTTPException(status_code=501, detail="Not implemented")

    async def cp(self, resource: status_models.Resource, user: User, request_model: models.PostCopyRequest) -> models.PostCopyResponse:
        raise HTTPException(status_code=501, detail="Not implemented")
