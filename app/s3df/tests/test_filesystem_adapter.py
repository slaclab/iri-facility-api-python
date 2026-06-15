"""Tests for the S3DF filesystem adapter (fs-facade-service backed)."""

import base64
import datetime
import json

import httpx
import pytest

from app.routers.filesystem import models
from app.routers.status import models as status_models
from app.s3df import filesystem_adapter as filesystem_adapter_module
from app.s3df.clients import fs_facade
from app.s3df.filesystem_adapter import S3DFFilesystemAdapter
from app.types.user import User


def _resource() -> status_models.Resource:
    return status_models.Resource(
        id="s3df-storage",
        name="Storage",
        description="storage",
        site_id="s3df",
        group="storage",
        resource_type=status_models.ResourceType.storage,
        current_status=status_models.Status.up,
        last_modified=datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc),
    )


def _user() -> User:
    return User(id="amithm", name="amithm", api_key="test", client_ip="127.0.0.1")


def _file_dict(name: str = "f.txt") -> dict:
    return {
        "name": name,
        "type": "file",
        "user": "amithm",
        "group": "amithm",
        "permissions": "rw-r--r--",
        "last_modified": "2026-06-01 12:00:00",
        "size": "10",
    }


def _install_handler(monkeypatch, handler):
    """Replace the global fs-facade client with one wired to a MockTransport."""
    transport = httpx.MockTransport(handler)
    client = fs_facade.FsFacadeClient(base_url="http://fs-facade.test", poll_interval=0, timeout=5)
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=transport)
    monkeypatch.setattr(filesystem_adapter_module, "get_fs_facade_client", lambda: client)
    return client


def _task_response(result, *, status: str = "completed", task_id: str = "t-1") -> httpx.Response:
    body = {
        "output": {
            "id": task_id,
            "status": status,
            "result": result,
            "command": None,
        }
    }
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_chmod_returns_file_model(monkeypatch):
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and req.url.path == "/filesystem/chmod":
            captured["body"] = json.loads(req.content)
            return httpx.Response(200, json="task-chmod")
        if req.url.path == "/task/task-chmod":
            return _task_response(json.dumps(_file_dict("a.txt")))
        return httpx.Response(404)

    _install_handler(monkeypatch, handler)
    adapter = S3DFFilesystemAdapter()
    response = await adapter.chmod(
        _resource(), _user(),
        models.PutFileChmodRequest(path="/sdf/data/a.txt", mode="755"),
    )
    assert isinstance(response, models.PutFileChmodResponse)
    assert response.output.name == "a.txt"
    assert captured["body"] == {"path": "/sdf/data/a.txt", "mode": "755"}


@pytest.mark.asyncio
async def test_ls_returns_list_of_files(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/filesystem/ls":
            return httpx.Response(200, json="task-ls")
        if req.url.path == "/task/task-ls":
            payload = json.dumps([_file_dict("a.txt"), _file_dict("b.txt")])
            return _task_response(payload)
        return httpx.Response(404)

    _install_handler(monkeypatch, handler)
    adapter = S3DFFilesystemAdapter()
    response = await adapter.ls(
        _resource(), _user(),
        path="/sdf/data", show_hidden=False,
        numeric_uid=False, recursive=False, dereference=False,
    )
    assert isinstance(response, models.GetDirectoryLsResponse)
    assert [f.name for f in response.output] == ["a.txt", "b.txt"]


@pytest.mark.asyncio
async def test_download_returns_base64_content(monkeypatch):
    payload = json.dumps({"content": base64.b64encode(b"hello").decode(), "size": 5})

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/filesystem/download":
            return httpx.Response(200, json="task-dl")
        if req.url.path == "/task/task-dl":
            return _task_response(payload)
        return httpx.Response(404)

    _install_handler(monkeypatch, handler)
    adapter = S3DFFilesystemAdapter()
    response = await adapter.download(_resource(), _user(), path="/sdf/data/a.txt")
    assert response.output == base64.b64encode(b"hello").decode()


@pytest.mark.asyncio
async def test_rm_returns_remove_response(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE" and req.url.path == "/filesystem/rm":
            return httpx.Response(200, json="task-rm")
        if req.url.path == "/task/task-rm":
            return _task_response(json.dumps({"status": "ok", "path": "/sdf/data/a.txt"}))
        return httpx.Response(404)

    _install_handler(monkeypatch, handler)
    adapter = S3DFFilesystemAdapter()
    response = await adapter.rm(_resource(), _user(), path="/sdf/data/a.txt")
    assert isinstance(response, models.RemoveResponse)
    assert "/sdf/data/a.txt" in response.output


@pytest.mark.asyncio
async def test_facade_5xx_maps_to_502(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _install_handler(monkeypatch, handler)
    adapter = S3DFFilesystemAdapter()
    with pytest.raises(Exception) as excinfo:
        await adapter.rm(_resource(), _user(), path="/sdf/data/a.txt")
    assert getattr(excinfo.value, "status_code", None) == 502


@pytest.mark.asyncio
async def test_failed_task_maps_to_502(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/filesystem/rm":
            return httpx.Response(200, json="task-fail")
        if req.url.path == "/task/task-fail":
            return _task_response("Error: permission denied", status="failed")
        return httpx.Response(404)

    _install_handler(monkeypatch, handler)
    adapter = S3DFFilesystemAdapter()
    with pytest.raises(Exception) as excinfo:
        await adapter.rm(_resource(), _user(), path="/sdf/data/a.txt")
    assert getattr(excinfo.value, "status_code", None) == 502
