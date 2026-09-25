"""Owner scoping and expiry for the S3DF task adapter."""

import httpx
import pytest

from app.routers.task import models as task_models
from app.s3df import task_adapter
from app.s3df.clients import fs_facade
from app.s3df.config import settings
from app.types.user import User


IDENTITIES = {
    "alice": {
        "x-auth-request-uid": "1001",
        "x-auth-request-primary-gid": "100",
        "x-auth-request-gids": "100",
    },
    "bob": {
        "x-auth-request-uid": "1002",
        "x-auth-request-primary-gid": "100",
        "x-auth-request-gids": "100",
    },
}


def _user(name: str) -> User:
    return User(id=name, name=name, api_key="test", client_ip="127.0.0.1")


def _completed(fs_task_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "output": {
                "id": fs_task_id,
                "status": "completed",
                "result": '{"output": "secret"}',
                "command": None,
            }
        },
    )


def _mock_client(handler) -> fs_facade.FsFacadeClient:
    client = fs_facade.FsFacadeClient(base_url="http://fs-facade.test", poll_interval=0, timeout=5)
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handler))
    return client


class _Facade:
    """Fake fs-facade. Records every /task call with the uid header it carried."""

    def __init__(self):
        self.user = "alice"
        self.submitted = 0
        self.task_calls: list[tuple[str, str, str | None]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/filesystem/"):
            self.submitted += 1
            return httpx.Response(200, json=f"fs-{self.submitted}")
        if req.url.path.startswith("/task/"):
            fs_task_id = req.url.path.removeprefix("/task/")
            self.task_calls.append((req.method, fs_task_id, req.headers.get("x-auth-request-uid")))
            return _completed(fs_task_id)
        return httpx.Response(404)


@pytest.fixture
def facade(monkeypatch):
    fake = _Facade()
    client = _mock_client(fake.handler)
    monkeypatch.setattr(task_adapter, "get_fs_facade_client", lambda: client)
    monkeypatch.setattr(task_adapter, "get_auth_headers", lambda: IDENTITIES[fake.user])
    monkeypatch.setattr(task_adapter.S3DFTaskAdapter, "_tasks", {})
    return fake


async def _submit(adapter, facade: _Facade, name: str) -> str:
    facade.user = name
    response = await adapter.put_task(
        user=_user(name),
        resource=None,
        task=task_models.TaskCommand(
            router="filesystem",
            command="tail",
            args={"path": f"/sdf/home/{name[0]}/{name}/out.txt"},
        ),
    )
    return response.task_id


@pytest.mark.asyncio
async def test_get_tasks_only_returns_and_fetches_callers_tasks(facade):
    adapter = task_adapter.S3DFTaskAdapter()
    alice_task = await _submit(adapter, facade, "alice")
    await _submit(adapter, facade, "bob")

    facade.user = "alice"
    tasks = await adapter.get_tasks(_user("alice"))

    assert [t.id for t in tasks] == [alice_task]
    assert facade.task_calls == [("GET", "fs-1", "1001")]


@pytest.mark.asyncio
async def test_other_users_task_cannot_be_read_or_deleted(facade):
    adapter = task_adapter.S3DFTaskAdapter()
    alice_task = await _submit(adapter, facade, "alice")

    facade.user = "bob"
    assert await adapter.get_task(_user("bob"), alice_task) is None
    await adapter.delete_task(_user("bob"), alice_task)
    assert facade.task_calls == []

    facade.user = "alice"
    task = await adapter.get_task(_user("alice"), alice_task)
    assert task.status == task_models.TaskStatus.completed
    assert task.result == {"output": "secret"}


@pytest.mark.asyncio
async def test_owner_delete_forwards_identity_and_forgets_task(facade):
    adapter = task_adapter.S3DFTaskAdapter()
    alice_task = await _submit(adapter, facade, "alice")

    await adapter.delete_task(_user("alice"), alice_task)

    assert facade.task_calls == [("DELETE", "fs-1", "1001")]
    assert await adapter.get_task(_user("alice"), alice_task) is None


@pytest.mark.asyncio
async def test_expired_tasks_are_forgotten(monkeypatch, facade):
    now = {"t": 1000.0}
    monkeypatch.setattr(task_adapter, "monotonic", lambda: now["t"])
    adapter = task_adapter.S3DFTaskAdapter()

    await _submit(adapter, facade, "alice")
    now["t"] += settings.fs_task_ttl - 10
    recent = await _submit(adapter, facade, "alice")
    now["t"] += 20

    tasks = await adapter.get_tasks(_user("alice"))

    assert [t.id for t in tasks] == [recent]
    assert list(task_adapter.S3DFTaskAdapter._tasks) == [recent]


@pytest.mark.asyncio
async def test_blocking_call_polls_task_with_identity_headers():
    polled_uids = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/filesystem/stat":
            return httpx.Response(200, json="fs-1")
        polled_uids.append(req.headers.get("x-auth-request-uid"))
        return _completed("fs-1")

    client = _mock_client(handler)
    await client.call("GET", "/filesystem/stat", params={"path": "/sdf/home/a/alice"}, headers=IDENTITIES["alice"])

    assert polled_uids == ["1001"]
