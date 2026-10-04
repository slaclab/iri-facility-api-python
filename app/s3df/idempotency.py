"""
S3DF idempotency store for compute submit/update.

Upstream ships no store: with IRI_IDEMPOTENCY_STORE unset, any request that
sends an Idempotency-Key gets 501. S3DF runs one replica with one process
(`fastapi run`), so an in-process store gives correct replay semantics:

    IRI_IDEMPOTENCY_STORE=app.s3df.idempotency.InMemoryIdempotencyStore

Entries are lost on restart, and a second replica or worker would not see
them. Switch to a shared store (e.g. Redis) before scaling out.
"""

import time

from app import config
from app.idempotency import IdempotencyStore, LockState

# Expired entries are only dropped when touched, so sweep periodically to keep
# memory bounded by the keys used within one TTL.
_SWEEP_INTERVAL_SECONDS = 60.0


class InMemoryIdempotencyStore(IdempotencyStore):
    """Process-local store; correct only while S3DF runs a single process."""

    def __init__(self, ttl: int | None = None, lock_ttl: int | None = None, clock=time.monotonic):
        self._ttl = config.IDEMPOTENCY_TTL_SECONDS if ttl is None else ttl
        self._lock_ttl = config.LOCK_TTL_SECONDS if lock_ttl is None else lock_ttl
        self._clock = clock
        self._entries: dict[str, tuple[dict, float]] = {}
        self._next_sweep = 0.0

    def _get(self, key: str) -> dict | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return value

    def _set(self, key: str, value: dict, ttl: int) -> None:
        self._entries[key] = (value, self._clock() + ttl)

    def _sweep(self) -> None:
        now = self._clock()
        if now < self._next_sweep:
            return
        self._next_sweep = now + _SWEEP_INTERVAL_SECONDS
        for key, (_, expires_at) in list(self._entries.items()):
            if now >= expires_at:
                del self._entries[key]

    # The methods below never await, so each runs atomically on the event loop.

    async def check_and_lock(self, cache_key: str, body_hash: str) -> tuple[str, dict | None, int | None]:
        self._sweep()
        data = self._get(cache_key)
        if data is None:
            self._set(cache_key, {"state": LockState.LOCKED, "body_hash": body_hash}, self._lock_ttl)
            return ("proceed", None, None)
        if data["state"] == LockState.LOCKED:
            return ("conflict", None, None)
        if data["body_hash"] != body_hash:
            return ("fingerprint_mismatch", None, None)
        return ("hit", data["response_body"], data["response_status"])

    async def store_result(self, cache_key: str, body_hash: str, response_body: dict, response_status: int) -> None:
        data = self._get(cache_key)
        if data is None or data["state"] != LockState.LOCKED or data["body_hash"] != body_hash:
            return
        self._set(
            cache_key,
            {
                "state": LockState.DONE,
                "body_hash": body_hash,
                "response_body": response_body,
                "response_status": response_status,
            },
            self._ttl,
        )

    async def delete_lock(self, cache_key: str) -> None:
        data = self._get(cache_key)
        if data is not None and data["state"] == LockState.LOCKED:
            del self._entries[cache_key]

    async def close(self) -> None:
        self._entries.clear()
