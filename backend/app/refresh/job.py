"""In-memory job state for the "Refresh Inventory" pipeline.

A single :class:`RefreshJob` is mutated by the pipeline as it advances and is
serialised by the API for the dashboard to poll. Keeping the state in a plain
dataclass (rather than the DB) matches the existing architecture: scrapers
already persist SKUs directly, so the job only needs to report progress.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.refresh.phases import PHASE_LABELS, PHASE_ORDER

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ShopResult:
    """Outcome of refreshing one shop in the current run.

    Two distinct durations are reported (both wall-clock seconds, ``None`` when
    unavailable):

    - ``scraper_seconds``: the scraper's *own* self-reported runtime, taken from
      the ``elapsed_seconds`` key of its stats dict. It covers only the scrape
      itself (catalogue fetch + per-product extraction + upsert) as measured by
      the scraper. ``None`` when the scraper failed before returning stats or
      did not report a timing.
    - ``total_seconds``: the pipeline's own monotonic measurement of the whole
      per-shop unit — the pre-scrape stock snapshot, the scraper run, and the
      post-scrape restock detection. It therefore includes snapshot/restock time
      the scraper cannot see, and is what the pipeline actually spent on the
      shop. ``None`` only if the shop never started.
    """

    shop: str
    ok: bool = False
    products_discovered: int = 0
    skus_inserted: int = 0
    skus_updated: int = 0
    skus_failed: int = 0
    error: Optional[str] = None
    scraper_seconds: Optional[float] = None
    total_seconds: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shop": self.shop,
            "ok": self.ok,
            "products_discovered": self.products_discovered,
            "skus_inserted": self.skus_inserted,
            "skus_updated": self.skus_updated,
            "skus_failed": self.skus_failed,
            "error": self.error,
            "scraper_seconds": self.scraper_seconds,
            "total_seconds": self.total_seconds,
        }


@dataclass
class RefreshJob:
    """Mutable state of the current (or last) refresh run."""

    id: str
    status: str = "running"
    phase: str = "initializing"
    message: str = PHASE_LABELS["initializing"]

    total_shops: int = 0
    completed_shops: int = 0
    current_shop: Optional[str] = None

    started_at: str = field(default_factory=_utcnow_iso)
    finished_at: Optional[str] = None
    error: Optional[str] = None
    cancelled: bool = False

    totals: Dict[str, int] = field(
        default_factory=lambda: {
            "products_discovered": 0,
            "skus_inserted": 0,
            "skus_updated": 0,
            "skus_failed": 0,
            "match_groups": 0,
            "matched_skus": 0,
            "back_in_stock": 0,
            "restocked_events": 0,
        }
    )
    shops: List[ShopResult] = field(default_factory=list)
    failed_shops: List[str] = field(default_factory=list)

    # Telemetry: completed-phase durations in seconds, keyed by phase name.
    # A phase is recorded when the pipeline moves past it (or when the run
    # settles while still inside it, e.g. a cancel mid-scrape — the value then
    # measures the time actually spent before stopping, not a full phase).
    phase_durations: Dict[str, float] = field(default_factory=dict)

    # Telemetry: the shared product-concurrency policy actually applied to this
    # run, e.g. ``{"global_limit": 6, "peak_active": 3, "per_shop": {...}}``.
    concurrency: Dict[str, Any] = field(default_factory=dict)

    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _finished_monotonic: Optional[float] = field(default=None, repr=False)
    _phase_started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _current_shop_started_monotonic: Optional[float] = field(default=None, repr=False)

    def set_phase(self, phase: str, message: Optional[str] = None) -> None:
        """Advance to ``phase`` and close out the previous phase's duration."""
        now = time.monotonic()
        if self.phase != phase:
            self.phase_durations[self.phase] = round(now - self._phase_started_monotonic, 2)
        self._phase_started_monotonic = now
        self.phase = phase
        self.message = message or PHASE_LABELS.get(phase, phase)

    def finish(self, status: str, error: Optional[str] = None) -> None:
        """Mark the run terminal and freeze its timing.

        Idempotent with respect to timing: the first call freezes the total
        duration and records the final phase, so later polls report stable
        values instead of a clock that keeps ticking.
        """
        now = time.monotonic()
        if self._finished_monotonic is None:
            self._finished_monotonic = now
        if self.phase not in self.phase_durations:
            self.phase_durations[self.phase] = round(
                max(0.0, self._finished_monotonic - self._phase_started_monotonic), 2
            )
        self.status = status
        self.error = error
        self.finished_at = _utcnow_iso()

    def begin_shop(self, shop_id: str) -> None:
        """Mark ``shop_id`` as the in-flight shop and start its live timer."""
        self.current_shop = shop_id
        self._current_shop_started_monotonic = time.monotonic()

    def end_shop(self) -> None:
        """Clear the in-flight shop and its live timer."""
        self.current_shop = None
        self._current_shop_started_monotonic = None

    @property
    def _clock(self) -> float:
        """The reference "now": frozen at finish, otherwise monotonic now."""
        return (
            self._finished_monotonic
            if self._finished_monotonic is not None
            else time.monotonic()
        )

    @property
    def elapsed_seconds(self) -> float:
        """Total refresh duration in seconds (frozen once terminal)."""
        return round(max(0.0, self._clock - self._started_monotonic), 2)

    @property
    def current_shop_elapsed_seconds(self) -> Optional[float]:
        """Live duration of the in-flight shop, or ``None`` when idle."""
        if self._current_shop_started_monotonic is None:
            return None
        return round(max(0.0, self._clock - self._current_shop_started_monotonic), 2)

    @property
    def progress(self) -> float:
        """Fraction in [0, 1] of the coarse phases completed.

        Each phase is one equal slice; within ``scraping`` the slice advances
        per completed shop so the bar tracks real work. There is no
        per-variant granularity because the scrapers do not expose it, and we
        deliberately avoid fabricating progress.
        """
        phases = [p for p, _ in PHASE_ORDER]
        if self.status in TERMINAL_STATUSES:
            return 1.0
        if self.phase == "scraping":
            within = self.completed_shops / self.total_shops if self.total_shops else 0.0
            return (phases.index("scraping") + within) / len(phases)
        idx = phases.index(self.phase) if self.phase in phases else len(phases) - 1
        return (idx + 1) / len(phases)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "phase": self.phase,
            "phase_label": PHASE_LABELS.get(self.phase, self.phase),
            "message": self.message,
            "progress": round(self.progress, 4),
            "total_shops": self.total_shops,
            "completed_shops": self.completed_shops,
            "current_shop": self.current_shop,
            "current_shop_elapsed_seconds": self.current_shop_elapsed_seconds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": self.elapsed_seconds,
            "phase_durations": dict(self.phase_durations),
            "concurrency": dict(self.concurrency),
            "error": self.error,
            "cancelled": self.cancelled,
            "totals": dict(self.totals),
            "shops": [s.to_dict() for s in self.shops],
            "failed_shops": list(self.failed_shops),
        }
