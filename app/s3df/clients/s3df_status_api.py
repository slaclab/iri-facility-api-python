"""
s3df-status-api Client

Async client for the S3DF status microservice. Returns raw JSON dicts to
keep the IRI status adapter responsible for shape conversion.

Endpoints (see ``s3df-status-api/app/controllers/status.py``):
  * ``GET /api/v1/resources``                       -> list[ResourceStatus]
  * ``GET /api/v1/resources/{resource_id}``         -> ResourceStatus
  * ``GET /api/v1/events?resource_id&since&limit``  -> list[StatusEvent]
  * ``GET /api/v1/incidents?resource_id&state``     -> list[Incident]

When events/incidents are disabled in the upstream service the relevant
endpoints return 404; we map that to an empty list so callers do not need
to handle it specially.
"""

import datetime
import logging
from typing import Optional

import httpx

from app.s3df.config import settings

LOG = logging.getLogger(__name__)


class S3DFStatusApiError(Exception):
    """Raised when the upstream returns an unexpected error."""


class S3DFStatusApiClient:
    """Async client for s3df-status-api."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or settings.s3df_status_api_url).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.s3df_status_api_timeout
        self._client: httpx.AsyncClient | None = None
        LOG.info(f"Initialized S3DFStatusApiClient for endpoint: {self.base_url}")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, *, params: dict | None = None) -> httpx.Response:
        client = self._get_client()
        try:
            return await client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise S3DFStatusApiError(f"s3df-status-api transport error: {exc}") from exc

    async def list_resource_statuses(
        self,
        group: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        params: dict = {}
        if group is not None:
            params["group"] = group
        if status is not None:
            params["status"] = status
        resp = await self._get("/api/v1/resources", params=params or None)
        if resp.status_code >= 400:
            raise S3DFStatusApiError(
                f"GET /resources -> {resp.status_code}: {resp.text}"
            )
        return resp.json()

    async def get_resource_status(self, resource_id: str) -> dict | None:
        resp = await self._get(f"/api/v1/resources/{resource_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise S3DFStatusApiError(
                f"GET /resources/{resource_id} -> {resp.status_code}: {resp.text}"
            )
        return resp.json()

    async def list_events(
        self,
        resource_id: str | None = None,
        since: datetime.datetime | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        params: dict = {"limit": limit}
        if resource_id is not None:
            params["resource_id"] = resource_id
        if since is not None:
            params["since"] = since.isoformat()
        resp = await self._get("/api/v1/events", params=params)
        if resp.status_code == 404:
            # events feature disabled upstream
            return []
        if resp.status_code >= 400:
            raise S3DFStatusApiError(
                f"GET /events -> {resp.status_code}: {resp.text}"
            )
        return resp.json()

    async def list_incidents(
        self,
        resource_id: str | None = None,
        state: str | None = None,
    ) -> list[dict]:
        params: dict = {}
        if resource_id is not None:
            params["resource_id"] = resource_id
        if state is not None:
            params["state"] = state
        resp = await self._get("/api/v1/incidents", params=params or None)
        if resp.status_code == 404:
            # incidents feature disabled upstream
            return []
        if resp.status_code >= 400:
            raise S3DFStatusApiError(
                f"GET /incidents -> {resp.status_code}: {resp.text}"
            )
        return resp.json()


_default_client: Optional[S3DFStatusApiClient] = None


def get_s3df_status_api_client() -> S3DFStatusApiClient:
    """Get or create the singleton S3DFStatusApiClient instance."""
    global _default_client
    if _default_client is None:
        _default_client = S3DFStatusApiClient()
    return _default_client
