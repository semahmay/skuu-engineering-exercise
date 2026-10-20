"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  PHASES,
  formatDuration,
  isActive,
  isTerminal,
  phaseState,
  resultLabel,
  titleizeShop,
  type RefreshJob,
  type RefreshStatus,
} from "@/lib/refresh";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const fmt = (n: number) => Math.round(n || 0).toLocaleString("en-US");

type Props = {
  open: boolean;
  onClose: () => void;
  /** Called once when a run reaches a terminal state (to reload metrics). */
  onFinished: () => void;
  /** Optional labeler for shop slugs; falls back to a titleized slug. */
  shopLabel?: (shopId: string) => string;
};

export default function RefreshPanel({
  open,
  onClose,
  onFinished,
  shopLabel = titleizeShop,
}: Props) {
  const [job, setJob] = useState<RefreshJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const notifiedRef = useRef<string | null>(null);
  const status = job?.status ?? "idle";

  /* Load the current state whenever the panel opens. */
  useEffect(() => {
    if (!open) return;
    let disposed = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/v1/refresh/status`, {
          cache: "no-store",
        });
        if (!res.ok) return;
        const data = (await res.json()) as RefreshStatus;
        if (!disposed && data.status !== "idle") setJob(data as RefreshJob);
      } catch {
        /* status is best-effort; the run itself is unaffected */
      }
    })();
    return () => {
      disposed = true;
    };
  }, [open]);

  /* Poll while a run is active. */
  useEffect(() => {
    if (!open || !isActive(status)) return;
    let disposed = false;
    const tick = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/v1/refresh/status`, {
          cache: "no-store",
        });
        if (!res.ok) return;
        const data = (await res.json()) as RefreshStatus;
        if (!disposed && data.status !== "idle") setJob(data as RefreshJob);
      } catch {
        /* transient network error — keep polling */
      }
    };
    tick();
    const id = window.setInterval(tick, 900);
    return () => {
      disposed = true;
      window.clearInterval(id);
    };
  }, [open, status]);

  /* Reload metrics once per finished run. */
  useEffect(() => {
    if (!job) return;
    if (isTerminal(job.status) && notifiedRef.current !== job.id) {
      notifiedRef.current = job.id;
      onFinished();
    }
  }, [job, onFinished]);

  const start = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/refresh`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `start failed (${res.status})`);
      }
      setJob((await res.json()) as RefreshJob);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not start refresh");
    } finally {
      setBusy(false);
    }
  }, []);

  const cancel = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/refresh/cancel`, {
        method: "POST",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `cancel failed (${res.status})`);
      }
      setJob((await res.json()) as RefreshJob);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not cancel refresh");
    } finally {
      setBusy(false);
    }
  }, []);

  if (!open) return null;

  const running = job ? isActive(job.status) : false;
  const pct = job ? Math.round(Math.max(0, Math.min(1, job.progress)) * 100) : 0;
  const t = job?.totals;

  return (
    <div
      className="rp-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Refresh inventory"
      onClick={(e) => {
        if (e.target === e.currentTarget && !running) onClose();
      }}
    >
      <div className="rp-card">
        <div className="rp-head">
          <div>
            <div className="eyebrow">Catalogue pipeline</div>
            <h3>Refresh inventory</h3>
          </div>
          <button
            className="rp-x"
            onClick={onClose}
            disabled={running}
            aria-label="Close"
            title={running ? "Cancel the run before closing" : "Close"}
          >
            ✕
          </button>
        </div>

        {!job && (
          <p className="rp-lead">
            Re-scrape every configured brand and retailer, persist new and
            updated SKUs, recompute cross-shop matches, and detect genuine
            back-in-stock transitions. This runs against the live catalogue and
            can take a while.
          </p>
        )}

        {job && (
          <>
            <div className="rp-status-row">
              <span className={`rp-pill ${job.status}`}>{resultLabel(job)}</span>
              <span className="rp-elapsed" title="Elapsed time">
                {formatDuration(job.elapsed_seconds)}
              </span>
            </div>

            <div className="rp-bar" aria-hidden="true">
              <i style={{ width: `${pct}%` }} />
              {running && <u style={{ animationDelay: "0s" }} />}
            </div>
            <div className="rp-bar-meta">
              <span>{job.message || job.phase_label}</span>
              <span>{pct}%</span>
            </div>

            {running && job.current_shop && (
              <div className="rp-bar-meta" title="Current shop and its elapsed time">
                <span>Now: {shopLabel(job.current_shop)}</span>
                <span>
                  {job.current_shop_elapsed_seconds != null
                    ? formatDuration(job.current_shop_elapsed_seconds)
                    : "—"}
                </span>
              </div>
            )}

            <ol className="rp-steps">
              {PHASES.map((phase) => {
                const state = phaseState(phase.key, job.phase, job.status);
                return (
                  <li key={phase.key} className={`rp-step ${state}`}>
                    <span className="rp-dot" />
                    <span>{phase.label}</span>
                  </li>
                );
              })}
            </ol>

            {t && (
              <div className="rp-totals">
                <div>
                  <small>New SKUs</small>
                  <b>{fmt(t.skus_inserted)}</b>
                </div>
                <div>
                  <small>Updated</small>
                  <b>{fmt(t.skus_updated)}</b>
                </div>
                <div>
                  <small>Restocked</small>
                  <b>{fmt(t.restocked_events)}</b>
                </div>
                <div>
                  <small>Shops</small>
                  <b>
                    {job.completed_shops}/{job.total_shops}
                  </b>
                </div>
              </div>
            )}

            {job.failed_shops.length > 0 && (
              <p className="rp-note">
                <b>{job.failed_shops.length}</b> shop
                {job.failed_shops.length > 1 ? "s" : ""} failed and were skipped:{" "}
                {job.failed_shops.join(", ")}
              </p>
            )}
            {job.error && <p className="rp-error">{job.error}</p>}
          </>
        )}

        {err && <p className="rp-error">{err}</p>}

        <div className="rp-actions">
          {!running ? (
            <button className="primary" onClick={start} disabled={busy}>
              {job && isTerminal(job.status) ? "Run again" : "Start refresh"}
            </button>
          ) : (
            <button className="danger" onClick={cancel} disabled={busy}>
              {job?.status === "cancelling" ? "Cancelling…" : "Cancel refresh"}
            </button>
          )}
          <button className="ghost" onClick={onClose} disabled={running}>
            {running ? "Running…" : "Close"}
          </button>
        </div>
      </div>
    </div>
  );
}
