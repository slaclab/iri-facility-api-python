import pytest
from fastapi import HTTPException

from app.routers.task import models as task_models
from app.s3df import task_adapter


def _command(command: str, args: dict) -> task_models.TaskCommand:
    return task_models.TaskCommand(
        router="filesystem",
        command=command,
        args=args,
    )


FILESYSTEM_COMMANDS = [
    ("file", {"path": "/sdf/home/a/test/file"}, "GET", "/filesystem/file"),
    ("stat", {"path": "/sdf/home/a/test/file"}, "GET", "/filesystem/stat"),
    ("ls", {"path": "/sdf/home/a/test"}, "GET", "/filesystem/ls"),
    ("head", {"path": "/sdf/home/a/test/file"}, "GET", "/filesystem/head"),
    ("tail", {"path": "/sdf/home/a/test/file"}, "GET", "/filesystem/tail"),
    ("view", {"path": "/sdf/home/a/test/file"}, "GET", "/filesystem/view"),
    ("checksum", {"path": "/sdf/home/a/test/file"}, "GET", "/filesystem/checksum"),
    ("download", {"path": "/sdf/home/a/test/file"}, "GET", "/filesystem/download"),
    ("rm", {"path": "/sdf/home/a/test/file"}, "DELETE", "/filesystem/rm"),
    (
        "mkdir",
        {"request_model": {"path": "/sdf/home/a/test", "parent": True}},
        "POST",
        "/filesystem/mkdir",
    ),
    (
        "symlink",
        {
            "request_model": {
                "path": "/sdf/home/a/test/file",
                "link_path": "/sdf/home/a/test/link",
            }
        },
        "POST",
        "/filesystem/symlink",
    ),
    (
        "compress",
        {
            "request_model": {
                "path": "/sdf/home/a/test",
                "target_path": "/sdf/home/a/test.tar.gz",
            }
        },
        "POST",
        "/filesystem/compress",
    ),
    (
        "extract",
        {
            "request_model": {
                "path": "/sdf/home/a/test.tar.gz",
                "target_path": "/sdf/home/a/extracted",
            }
        },
        "POST",
        "/filesystem/extract",
    ),
    (
        "mv",
        {
            "request_model": {
                "path": "/sdf/home/a/test/file",
                "target_path": "/sdf/home/a/test/moved",
            }
        },
        "POST",
        "/filesystem/mv",
    ),
    (
        "cp",
        {
            "request_model": {
                "path": "/sdf/home/a/test/file",
                "target_path": "/sdf/home/a/test/copied",
            }
        },
        "POST",
        "/filesystem/cp",
    ),
    (
        "upload",
        {"path": "/sdf/home/a/test/file", "content": "aGVsbG8="},
        "POST",
        "/filesystem/upload",
    ),
    (
        "chmod",
        {"request_model": {"path": "/sdf/home/a/test/file", "mode": "755"}},
        "PUT",
        "/filesystem/chmod",
    ),
    (
        "chown",
        {"request_model": {"path": "/sdf/home/a/test/file", "owner": "test"}},
        "PUT",
        "/filesystem/chown",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "args", "expected_method", "expected_path"),
    FILESYSTEM_COMMANDS,
)
async def test_filesystem_commands_forward_request_auth_headers(
    monkeypatch,
    command,
    args,
    expected_method,
    expected_path,
):
    captured = {}
    auth_headers = {
        "x-auth-request-uid": "47964",
        "x-auth-request-primary-gid": "100",
        "x-auth-request-gids": "100,200",
    }

    class CapturingClient:
        async def submit(self, method, path, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["kwargs"] = kwargs
            return "fs-task-id"

    monkeypatch.setattr(task_adapter, "get_fs_facade_client", CapturingClient)
    monkeypatch.setattr(task_adapter, "get_auth_headers", lambda: auth_headers)

    task_id = await task_adapter._submit_to_fs_facade(_command(command, args))

    assert task_id == "fs-task-id"
    assert captured["method"] == expected_method
    assert captured["path"] == expected_path
    assert captured["kwargs"]["headers"] == auth_headers


@pytest.mark.asyncio
async def test_primary_gid_is_forwarded_first(monkeypatch):
    captured = {}
    auth_headers = {
        "x-auth-request-uid": "47964",
        "x-auth-request-primary-gid": "200",
        "x-auth-request-gids": "100,200,100,300,200",
    }

    class CapturingClient:
        async def submit(self, method, path, **kwargs):
            captured["headers"] = kwargs["headers"]
            return "fs-task-id"

    monkeypatch.setattr(task_adapter, "get_fs_facade_client", CapturingClient)
    monkeypatch.setattr(task_adapter, "get_auth_headers", lambda: auth_headers)

    await task_adapter._submit_to_fs_facade(
        _command(
            "mkdir",
            {"request_model": {"path": "/sdf/home/a/test", "parent": True}},
        )
    )

    assert captured["headers"]["x-auth-request-gids"] == "200,100,300"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_headers",
    [
        {},
        {"x-auth-request-primary-gid": "100"},
        {"x-auth-request-uid": "47964"},
    ],
)
async def test_missing_auth_headers_are_returned_as_401_before_facade_call(
    monkeypatch,
    auth_headers,
):
    monkeypatch.setattr(
        task_adapter,
        "get_fs_facade_client",
        lambda: pytest.fail("missing identity reached the fs-facade client"),
    )
    monkeypatch.setattr(task_adapter, "get_auth_headers", lambda: auth_headers)

    with pytest.raises(HTTPException) as exc_info:
        await task_adapter.S3DFTaskAdapter().put_task(
            user=None,
            resource=None,
            task=_command(
                "mkdir",
                {"request_model": {"path": "/sdf/home/a/test", "parent": True}},
            ),
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_headers",
    [
        {
            "x-auth-request-uid": "not-a-uid",
            "x-auth-request-primary-gid": "100",
        },
        {
            "x-auth-request-uid": "47964",
            "x-auth-request-primary-gid": "not-a-gid",
        },
        {
            "x-auth-request-uid": "47964",
            "x-auth-request-primary-gid": "100",
            "x-auth-request-gids": "100,not-a-gid",
        },
    ],
)
async def test_invalid_identity_headers_are_returned_as_400(
    monkeypatch,
    auth_headers,
):
    monkeypatch.setattr(
        task_adapter,
        "get_fs_facade_client",
        lambda: pytest.fail("invalid identity reached the fs-facade client"),
    )
    monkeypatch.setattr(task_adapter, "get_auth_headers", lambda: auth_headers)

    with pytest.raises(HTTPException) as exc_info:
        await task_adapter.S3DFTaskAdapter().put_task(
            user=None,
            resource=None,
            task=_command(
                "mkdir",
                {"request_model": {"path": "/sdf/home/a/test", "parent": True}},
            ),
        )

    assert exc_info.value.status_code == 400
