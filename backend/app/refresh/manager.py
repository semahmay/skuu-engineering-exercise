"""Process-wide manager for the single "Refresh Inventory" job.

Only one refresh may run at a time; a second start is rejected rather than
queued. The manager owns the cancel signal and the background task, and exposes
the job state for the status endpoint. It intentionally holds no database state
of its own.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional
from uuid import uuid4

from app.refresh.job import RefreshJob
from app.refresh.pipeline import run_refresh

ACTIVE_STATUSES = {"running", "cancelling"}


class RefreshError(Exception):
    """Base class for refresh control errors."""


class RefreshInProgress(RefreshError):
    """Raised when a refresh is already running."""


class NoActiveRefresh(RefreshError):
    """Raised when cancelling or querying with no active refresh."""


class RefreshManager:
    def __init__(self) -> None:
        self._job: Optional[RefreshJob] = None
        self._task: Optional[asyncio.Task] = None
        self._cancel_event: Optional[asyncio.Event] = None
        self._lock = asyncio.Lock()

    @property
    def job(self) -> Optional[RefreshJob]:
        return self._job

    def is_active(self) -> bool:
        return self._job is not None and self._job.status in ACTIVE_STATUSES

    async def start(self, **run_kwargs: Any) -> RefreshJob:
        """Start a refresh; raises :class:`RefreshInProgress` if one is active."""
        async with self._lock:
            if self.is_active():
                raise RefreshInProgress("a refresh is already running")
            job = RefreshJob(id=uuid4().hex)
            self._job = job
            self._cancel_event = asyncio.Event()
            self._task = asyncio.create_task(
                run_refresh(job, cancel_event=self._cancel_event, **run_kwargs)
            )
            return job

    async def cancel(self) -> RefreshJob:
        """Signal the running refresh to stop; state becomes ``cancelling``."""
        async with self._lock:
            if self._job is None or self._job.status not in ACTIVE_STATUSES:
                raise NoActiveRefresh("no refresh is running")
            if self._cancel_event is not None:
                self._cancel_event.set()
            if self._job.status == "running":
                self._job.status = "cancelling"
                self._job.message = "Cancelling refresh"
            return self._job

    def status(self) -> Optional[Dict[str, Any]]:
        return self._job.to_dict() if self._job is not None else None

    async def wait(self) -> Optional[RefreshJob]:
        """Await the background task (used by tests); returns the job."""
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        return self._job

    def reset(self) -> None:
        """Drop all state (test helper). Does not cancel a running task."""
        self._job = None
        self._task = None
        self._cancel_event = None


# Module-level singleton used by the API layer.
manager = RefreshManager()
