"""
Background poller for the S3DF status adapter.

Periodically runs the health check for each monitored resource and feeds the
results into the :class:`~app.s3df.status.store.StatusStore`. The ``httpx``
client is created lazily inside the running event loop (see :meth:`start`).
"""

import asyncio
import logging

import httpx

from .config import MonitoredResource, StatusSettings
from .health_checker import HealthChecker
from .store import StatusStore

logger = logging.getLogger(__name__)


class StatusPoller:
    """Periodically runs health checks and feeds results into the store."""

    def __init__(self, store: StatusStore, settings: StatusSettings, monitored: list[MonitoredResource]):
        self.store = store
        self.settings = settings
        self.monitored = monitored
        self._client: httpx.AsyncClient | None = None
        self._checker: HealthChecker | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Create the HTTP client (inside the running loop), run one initial poll,
        then launch the periodic background loop."""
        self._client = httpx.AsyncClient(verify=self.settings.tls_verify)
        self._checker = HealthChecker(self.settings, self._client)
        await self._poll_once()  # bounded by per-query timeouts; never raises
        self._task = asyncio.create_task(self._run(), name="s3df-status-poller")
        logger.info(
            "S3DF status poller started (%d resources, interval=%ss)",
            len(self.monitored),
            self.settings.poll_interval,
        )

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.poll_interval)
                await self._poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the server alive if the loop dies
            logger.exception("S3DF status poller loop crashed")

    async def _poll_once(self) -> None:
        assert self._checker is not None
        monitored = self.monitored
        results = await asyncio.gather(
            *(self._checker.check(m.health_check) for m in monitored),
            return_exceptions=True,
        )
        for m, res in zip(monitored, results):
            if isinstance(res, Exception):
                logger.warning("Health check raised for %s: %s", m.resource.id, res)
                continue
            self.store.record(m.resource.id, res)

    async def aclose(self) -> None:
        """Cancel the loop and close the HTTP client. For tests/lifespan wiring."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
