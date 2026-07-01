"""
fs-facade-service Client

Async client for the filesystem facade microservice. Submits a filesystem
operation, polls the task endpoint until terminal state, and returns the
parsed JSON ``result`` payload.

The microservice exposes a task-queue API:
  * ``POST/GET/PUT/DELETE /filesystem/{op}`` -> returns a bare ``task_id`` string
  * ``GET /task/{task_id}`` -> returns ``{"output": Task{id,status,result,command}}``
    where ``result`` is itself a JSON-encoded string (set by the dispatcher).

See: ``fs-facade-service/app/controllers/{filesystem_controller,task_controller}.py``.
"""

import asyncio
import json
import logging
from typing import Any, Optional

import httpx

from app.s3df.config import settings

LOG = logging.getLogger(__name__)


class FsFacadeError(Exception):
    """Raised when fs-facade returns an error or a task fails."""
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class FsFacadeTimeout(Exception):
    """Raised when polling exceeds the configured timeout."""


_TERMINAL = {"completed", "failed", "canceled"}


class FsFacadeClient:
    """Async client for fs-facade-service."""

    def __init__(
        self,
        base_url: str | None = None,
        poll_interval: float | None = None,
        timeout: float | None = None,
    ):
        self.base_url = (base_url or settings.fs_facade_url).rstrip("/")
        self.poll_interval = poll_interval if poll_interval is not None else settings.fs_facade_poll_interval
        self.timeout = timeout if timeout is not None else settings.fs_facade_timeout
        self._client: httpx.AsyncClient | None = None
        LOG.info(f"Initialized FsFacadeClient for endpoint: {self.base_url}")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request_task_id(self, method: str, path: str, **kwargs) -> str:
        """Issue an HTTP request that returns a bare task_id string."""
        client = self._get_client()
        try:
            resp = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise FsFacadeError(f"fs-facade transport error: {exc}") from exc
        if resp.status_code >= 400:
            raise FsFacadeError(
                f"fs-facade {method} {path} -> {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
            )
        data = resp.json()
        # The controllers return either a bare string (response_model=str) or
        # `{"task_id": "..."}` (the create_task endpoint). Accept both shapes.
        if isinstance(data, str):
            return data
        if isinstance(data, dict) and "task_id" in data:
            return data["task_id"]
        raise FsFacadeError(f"fs-facade returned unexpected payload: {data!r}")

    async def get_task(self, task_id: str) -> dict:
        """Fetch the current task record."""
        client = self._get_client()
        try:
            resp = await client.get(f"/task/{task_id}")
        except httpx.HTTPError as exc:
            raise FsFacadeError(f"fs-facade transport error: {exc}") from exc
        if resp.status_code == 404:
            raise FsFacadeError(f"fs-facade task not found: {task_id}")
        if resp.status_code >= 400:
            raise FsFacadeError(
                f"fs-facade GET /task/{task_id} -> {resp.status_code}: {resp.text}"
            )
        body = resp.json()
        # Wrapped in `{"output": Task{...}}` per the task_controller.
        return body.get("output", body) if isinstance(body, dict) else body

    async def wait(self, task_id: str, timeout: float | None = None) -> dict:
        """Poll until the task reaches a terminal state. Returns the Task dict."""
        deadline_left = timeout if timeout is not None else self.timeout
        elapsed = 0.0
        while True:
            task = await self.get_task(task_id)
            status = task.get("status")
            if status in _TERMINAL:
                return task
            if elapsed >= deadline_left:
                raise FsFacadeTimeout(
                    f"Timed out waiting for fs-facade task {task_id} (status={status})"
                )
            await asyncio.sleep(self.poll_interval)
            elapsed += self.poll_interval

    async def call(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any | None = None,
        files: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Submit an operation, wait for terminal state, and return the parsed result.

        On success returns the JSON-decoded ``result`` (or the raw string when
        the dispatcher returned a non-JSON payload). On failure raises
        ``FsFacadeError``.
        """
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        if files is not None:
            kwargs["files"] = files
        if data is not None:
            kwargs["data"] = data
        if headers is not None:
            kwargs["headers"] = headers

        task_id = await self._request_task_id(method, path, **kwargs)
        task = await self.wait(task_id, timeout=timeout)

        status = task.get("status")
        result = task.get("result")
        if status != "completed":
            raise FsFacadeError(
                f"fs-facade task {task_id} ended with status={status}: {result}"
            )

        if isinstance(result, str):
            try:
                return json.loads(result)
            except (TypeError, ValueError):
                return result
        return result

    async def submit(
        self,
        method: str,
        path: str,
        *, #TODO: Get rid of the *
        params: dict | None = None,
        json_body: Any | None = None,
        files: dict | None = None,
        headers: dict | None = None,
    ) -> str:
        """Submit an operation to fs-facade and return the task_id immediately, without polling."""
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        if files is not None:
            kwargs["files"] = files
        if headers is not None:
            kwargs["headers"] = headers
        return await self._request_task_id(method, path, **kwargs)


_default_client: Optional[FsFacadeClient] = None


def get_fs_facade_client() -> FsFacadeClient:
    """Get or create the singleton FsFacadeClient instance."""
    global _default_client
    if _default_client is None:
        _default_client = FsFacadeClient()
    return _default_client
