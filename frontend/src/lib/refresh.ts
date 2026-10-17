/**
 * Pure helpers for the "Refresh Inventory" panel.
 *
 * Kept framework-free so the node test runner can exercise them without a DOM.
 */

export type RefreshTotals = {
  products_discovered: number;
  skus_inserted: number;
  skus_updated: number;
  skus_failed: number;
  match_groups: number;
  matched_skus: number;
  back_in_stock: number;
  restocked_events: number;
};

export type RefreshShopResult = {
  shop: string;
  ok: boolean;
  products_discovered: number;
  skus_inserted: number;
  skus_updated: number;
  skus_failed: number;
  error?: string | null;
  /** Scraper's self-reported runtime (seconds), if it reported one. */
  scraper_seconds?: number | null;
  /** Pipeline wall-clock for the whole shop unit: snapshot + scrape + restock. */
  total_seconds?: number | null;
};

export type RefreshJob = {
  id: string;
  status: string;
  phase: string;
  phase_label: string;
  message: string;
  progress: number;
  total_shops: number;
  completed_shops: number;
  current_shop: string | null;
  /** Live elapsed time of the in-flight shop, or null when idle. */
  current_shop_elapsed_seconds?: number | null;
  started_at: string;
  finished_at: string | null;
  elapsed_seconds: number;
  /** Completed phase durations in seconds, keyed by phase name. */
  phase_durations?: Record<string, number>;
  error: string | null;
  cancelled: boolean;
  totals: RefreshTotals;
  shops: RefreshShopResult[];
  failed_shops: string[];
};

export type RefreshStatus = RefreshJob | { status: "idle" };

/** The pipeline phases, in order, matching the backend. */
export const PHASES: { key: string; label: string }[] = [
  { key: "initializing", label: "Initializing refresh" },
  { key: "scraping", label: "Scraping brands and retailers" },
  { key: "saving", label: "Saving new and updated SKUs" },
  { key: "matching", label: "Matching products" },
  { key: "back_in_stock", label: "Checking back-in-stock opportunities" },
  { key: "metrics", label: "Updating dashboard metrics" },
];

export const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

export const isActive = (status: string) =>
  status === "running" || status === "cancelling";

export const isTerminal = (status: string) => TERMINAL_STATUSES.has(status);

/** Fallback shop label: turns a slug (`carhartt_wip`) into `Carhartt Wip`. */
export function titleizeShop(slug: string): string {
  return slug
    .split(/[\s_]+/)
    .filter(Boolean)
    .map((w) => w[0].toUpperCase() + w.slice(1))
    .join(" ");
}

export function phaseIndex(phase: string): number {
  return PHASES.findIndex((p) => p.key === phase);
}

export type PhaseState = "done" | "active" | "todo";

/** Where a step sits relative to the job's current phase. */
export function phaseState(
  phase: string,
  activePhase: string,
  status: string,
): PhaseState {
  if (status === "completed") return "done";
  const active = phaseIndex(activePhase);
  const current = phaseIndex(phase);
  if (isTerminal(status)) {
    if (current < active) return "done";
    if (current === active) return "active";
    return "todo";
  }
  if (current < active) return "done";
  if (current === active) return "active";
  return "todo";
}

/** ``mm:ss`` (or ``h:mm:ss`` past an hour) from a number of seconds. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** Human summary for a finished run. */
export function resultLabel(job: RefreshJob): string {
  if (job.status === "completed") {
    const refreshed = job.total_shops - job.failed_shops.length;
    return refreshed === job.total_shops
      ? "Inventory refreshed"
      : `Partial refresh (${refreshed}/${job.total_shops} shops)`;
  }
  if (job.status === "cancelled") return "Refresh cancelled";
  if (job.status === "failed") return "Refresh failed";
  return job.phase_label;
}
