import { test } from "node:test";
import assert from "node:assert/strict";

import {
  PHASES,
  formatDuration,
  isActive,
  isTerminal,
  phaseIndex,
  phaseState,
  resultLabel,
  titleizeShop,
} from "../src/lib/refresh.ts";
import type { RefreshJob } from "../src/lib/refresh.ts";

test("PHASES order matches the pipeline", () => {
  assert.deepEqual(
    PHASES.map((p) => p.key),
    ["initializing", "scraping", "saving", "matching", "back_in_stock", "metrics"],
  );
  assert.equal(phaseIndex("scraping"), 1);
  assert.equal(phaseIndex("nope"), -1);
});

test("isActive / isTerminal classify statuses", () => {
  assert.equal(isActive("running"), true);
  assert.equal(isActive("cancelling"), true);
  assert.equal(isActive("completed"), false);
  assert.equal(isTerminal("completed"), true);
  assert.equal(isTerminal("failed"), true);
  assert.equal(isTerminal("cancelled"), true);
  assert.equal(isTerminal("running"), false);
});

test("phaseState marks done/active/todo while running", () => {
  assert.equal(phaseState("initializing", "matching", "running"), "done");
  assert.equal(phaseState("matching", "matching", "running"), "active");
  assert.equal(phaseState("metrics", "matching", "running"), "todo");
});

test("finished runs mark every phase done or the halted phase active", () => {
  for (const p of PHASES) {
    assert.equal(phaseState(p.key, "scraping", "completed"), "done");
  }
  assert.equal(phaseState("initializing", "scraping", "cancelled"), "done");
  assert.equal(phaseState("scraping", "scraping", "cancelled"), "active");
  assert.equal(phaseState("metrics", "scraping", "cancelled"), "todo");
});

test("formatDuration renders mm:ss and h:mm:ss", () => {
  assert.equal(formatDuration(0), "0:00");
  assert.equal(formatDuration(9), "0:09");
  assert.equal(formatDuration(65), "1:05");
  assert.equal(formatDuration(600), "10:00");
  assert.equal(formatDuration(3661), "1:01:01");
  assert.equal(formatDuration(-4), "0:00");
});

const baseJob: RefreshJob = {
  id: "1",
  status: "completed",
  phase: "metrics",
  phase_label: "Updating dashboard metrics",
  message: "",
  progress: 1,
  total_shops: 13,
  completed_shops: 13,
  current_shop: null,
  started_at: "",
  finished_at: "",
  elapsed_seconds: 12,
  error: null,
  cancelled: false,
  totals: {
    products_discovered: 0,
    skus_inserted: 0,
    skus_updated: 0,
    skus_failed: 0,
    match_groups: 0,
    matched_skus: 0,
    back_in_stock: 0,
    restocked_events: 0,
  },
  shops: [],
  failed_shops: [],
};

test("resultLabel distinguishes full, partial, cancelled and failed runs", () => {
  assert.equal(resultLabel(baseJob), "Inventory refreshed");
  assert.equal(
    resultLabel({ ...baseJob, failed_shops: ["biffi"] }),
    "Partial refresh (12/13 shops)",
  );
  assert.equal(resultLabel({ ...baseJob, status: "cancelled" }), "Refresh cancelled");
  assert.equal(resultLabel({ ...baseJob, status: "failed" }), "Refresh failed");
});

test("titleizeShop is a friendly fallback for shop slugs", () => {
  assert.equal(titleizeShop("carhartt_wip"), "Carhartt Wip");
  assert.equal(titleizeShop("ssense"), "Ssense");
  assert.equal(titleizeShop("nugnes_1920"), "Nugnes 1920");
  assert.equal(titleizeShop(""), "");
});

test("timing telemetry fields are optional and don't affect summaries", () => {
  const job: RefreshJob = {
    ...baseJob,
    current_shop: "ssense",
    current_shop_elapsed_seconds: 12.4,
    phase_durations: { scraping: 3.2, matching: 0.4 },
    shops: [
      {
        shop: "ssense",
        ok: true,
        products_discovered: 1,
        skus_inserted: 1,
        skus_updated: 0,
        skus_failed: 0,
        scraper_seconds: 120.5,
        total_seconds: 122.1,
      },
    ],
  };
  assert.equal(resultLabel(job), "Inventory refreshed");
  assert.equal(job.current_shop_elapsed_seconds, 12.4);
  assert.equal(job.shops[0].scraper_seconds, 120.5);
  assert.equal(job.shops[0].total_seconds, 122.1);
  assert.equal(formatDuration(job.current_shop_elapsed_seconds ?? 0), "0:12");
});
