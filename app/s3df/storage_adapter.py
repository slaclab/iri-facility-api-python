"""Static S3DF implementation of the IRI storage discovery API."""

from fastapi import HTTPException

from app.routers.status import models as status_models
from app.routers.storage import facility_adapter, models as storage_models
from app.s3df.auth.authenticated_adapter import S3DFAuthenticatedAdapter
from app.types.user import User


_SDFHOME_RESOURCE_ID = "sdfhome"
_SDFHOME_FILESYSTEM = "sdfhome"


def _username(user: User) -> str:
    """Return a path-safe authenticated username."""
    username = getattr(user, "unix_username", None) or user.id
    if not username or username in {".", ".."} or "/" in username or "\x00" in username:
        raise HTTPException(
            status_code=500,
            detail="Authenticated user cannot be mapped to an S3DF home directory",
        )
    return username


def _require_sdfhome(resource: status_models.Resource) -> None:
    """Reject resources for which S3DF cannot provide static storage discovery."""
    if (
        resource.id != _SDFHOME_RESOURCE_ID
        or resource.resource_type != status_models.ResourceType.storage
    ):
        raise HTTPException(
            status_code=501,
            detail=f"Storage discovery is not implemented for resource '{resource.id}'",
        )


class S3DFStorageAdapter(S3DFAuthenticatedAdapter, facility_adapter.FacilityAdapter):
    """Expose declared S3DF home storage without dynamic filesystem discovery."""

    async def get_user(
        self,
        user_id: str,
        api_key: str,
        client_ip: str | None,
        globus_introspect: dict | None = None,
    ) -> User:
        return User(
            id=user_id,
            name=user_id,
            api_key=api_key,
            client_ip=client_ip,
        )

    async def get_locations(
        self,
        resource: status_models.Resource,
        user: User,
        logicalpath: storage_models.LogicalName | None,
        project: str | None,
        allocation: str | None,
        intent: storage_models.StorageIntent | None,
    ) -> list[storage_models.StorageInstance]:
        """Return the declared S3DF home path for the authenticated user."""
        _require_sdfhome(resource)

        if project is not None or allocation is not None:
            raise HTTPException(
                status_code=501,
                detail="Project and allocation storage discovery is not implemented at S3DF",
            )

        if logicalpath not in (None, storage_models.LogicalName.home):
            return []
        if intent == storage_models.StorageIntent.long_term_storage:
            return []

        username = _username(user)
        return [
            storage_models.StorageInstance(
                logical_name=storage_models.LogicalName.home,
                path=f"/sdf/home/{username[0]}/{username}",
                filesystem=_SDFHOME_FILESYSTEM,
                performance_tier=None,
                purge_policy_days=None,
                shared=False,
                access=storage_models.AccessPermissions(
                    read=True,
                    write=True,
                    execute=True,
                ),
            )
        ]

    async def get_access_endpoints(
        self,
        resource: status_models.Resource,
        user: User,
        protocol: storage_models.AccessProtocol | None,
        endpoint_id: str | None,
    ) -> list[storage_models.AccessEndpoint]:
        """Return configured remote endpoints; none are approved initially."""
        _require_sdfhome(resource)
        return []