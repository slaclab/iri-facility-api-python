"""
S3DF Task Adapter

Executes IRI tasks inline by forwarding to the facility adapter's on_task()
handler, which routes filesystem commands through S3DFFilesystemAdapter →
FsFacadeClient → fs-facade-service. Results are stored in-process and
retrievable via get_task().
"""

import uuid
import logging

from app.s3df.auth.authenticated_adapter import S3DFAuthenticatedAdapter
from app.routers.task import facility_adapter as task_adapter, models as task_models
from app.routers.status import models as status_models
from app.types.user import User

LOG = logging.getLogger(__name__)


class S3DFTaskAdapter(S3DFAuthenticatedAdapter, task_adapter.FacilityAdapter):
    """Task adapter that executes commands inline and stores results in-process."""

    def __init__(self):
        self._tasks: dict[str, task_models.Task] = {}

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
        task_id = str(uuid.uuid4())
        LOG.info("Executing task %s inline: %s:%s", task_id, task.router, task.command)
        result, status = await self.on_task(resource, user, task)
        if hasattr(result, "model_dump"):
            result_dict = result.model_dump()
        elif isinstance(result, dict):
            result_dict = result
        else:
            result_dict = {"output": result}
        self._tasks[task_id] = task_models.Task(
            id=task_id, status=status, result=result_dict, command=task
        )
        return task_models.TaskSubmitResponse(task_id=task_id)

    async def get_task(self, user: User, task_id: str) -> task_models.Task | None:
        return self._tasks.get(task_id)

    async def get_tasks(self, user: User) -> list[task_models.Task]:
        return list(self._tasks.values())

    async def delete_task(self, user: User, task_id: str) -> None:
        self._tasks.pop(task_id, None)
