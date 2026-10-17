"""Refresh Inventory API — start, poll and cancel the refresh pipeline.

The dashboard starts a run, polls status while it progresses, and can cancel it.
Only one run may be active at a time. The actual work happens in a background
task owned by :data:`app.refresh.manager`.
"""

from fastapi import APIRouter, HTTPException

from app.refresh import NoActiveRefresh, RefreshInProgress, manager

router = APIRouter(tags=["refresh"])


@router.post("/refresh", status_code=202)
async def start_refresh() -> dict:
    """Start a full catalogue refresh. 409 if one is already running."""
    try:
        job = await manager.start()
    except RefreshInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.to_dict()


@router.get("/refresh/status")
async def refresh_status() -> dict:
    """Current (or last) run state; ``{"status": "idle"}`` if never run."""
    state = manager.status()
    if state is None:
        return {"status": "idle"}
    return state


@router.post("/refresh/cancel", status_code=202)
async def cancel_refresh() -> dict:
    """Request cancellation of the active run. 409 if nothing is running."""
    try:
        job = await manager.cancel()
    except NoActiveRefresh as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.to_dict()
