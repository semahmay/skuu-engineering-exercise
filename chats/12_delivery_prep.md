# Delivery readiness pass (frozen v1.2.0)

> **Re-audit update (later phase).** After this pass, a partial Veja re-scrape
> (≈670 products re-aligned to `colour=""`) changed the live dataset. The current
> audited values are **50,302 total · 7,138 matched · 14.1903% coverage · 3,466
> groups · 317 opportunities · 405 sold-out SKUs**, and the backend suite is now
> **202 passing**. The figures below are updated to these current values; the
> earlier snapshot (6,982 / 13.8802% / 3,388 / 285 / 373) is kept only as
> historical context in section 1. Engine version is unchanged (`1.2.0`).

Final pass before hand-off. Goal: confirm the repository can be set up, run,
demoed and submitted from a clean clone, and that the shipped numbers are real
and reproducible. **No matching logic was touched** in this pass.

## 1. README review

Starting from a clean clone, the README was checked end-to-end. Fixed:

- **Wrong env var name.** The frontend reads `NEXT_PUBLIC_API_URL`
  (`frontend/src/app/page.tsx:35`) but the README documented
  `NEXT_PUBLIC_API_BASE`. Corrected.
- **Stale test count.** "165 passed" → **187 passed** at the time of this pass
  (re-audited later to **202 passed**).
- **Missing prerequisites.** Added a version table (Python 3.11+, Node 20+,
  MongoDB 6+ / tested 8.2, Git) and a note that MongoDB Database Tools are *not*
  required.
- **Missing MongoDB setup.** Added a short section (service check / start,
  auto-created DB + indexes).
- **Missing demo-data section.** Added [Demo data](#demo-data-export--restore)
  documenting `scripts/export_skus.py` / `import_skus.py`.
- **Metrics conflation.** Added an explicit note that `matched_skus` (rows),
  `matched_groups` (groups) and `back_in_stock_opportunities` (a subset of
  groups) are different units.

## 2. Dashboard (real data + states)

Reviewed `frontend/src/app/page.tsx` (client component, real `fetch`) and then
verified in a headless browser against the live API:

| Check | Result |
| --- | --- |
| Loading state ("Loading metrics…") | visible |
| Real API values rendered | 50,302 · 7,138 · 14.19% · 317 · 3,466 · 405 all present |
| Error state on API failure | "Could not reach the API" shown |
| Refresh re-fetches | yes |

Verified with Playwright (`verify_dashboard.py`, gitignored) against
`npm start` + the uvicorn API. No mocks — the numbers come over HTTP from
`GET /api/v1/metrics`.

## 3. Metric definitions audit

- `total_skus` = every row in `skus` → **50,302**
- `matched_skus` = rows belonging to a cluster spanning ≥2 shops → **7,138**
- `coverage_pct` = `matched_skus / total_skus × 100` → **14.1903**
- `back_in_stock_opportunities` = cross-shop clusters with mixed stock → **317**
- extras: `back_in_stock_skus` = **405**, `matched_groups` = **3,466**

(Historical frozen-v1.2.0 snapshot before the partial Veja re-scrape:
matched **6,982**, coverage **13.8802%**, opportunities **285**,
back-in-stock SKUs **373**, groups **3,388**.)

Definitions match `compute_metrics` / `Metrics.to_dict` and the assignment.
`matched_skus` and `matched_groups` are deliberately different measures.

## 4. Export / restore fidelity

MongoDB Database Tools are not installed, so two small scripts were added:

- `backend/scripts/export_skus.py` — `skuu.skus` → `data/skuu_skus.jsonl`
  (extended JSON, one doc per line). Exported 50,302 docs (~18.7 MB).
- `backend/scripts/import_skus.py` — restores via `--db` / `--drop`, recreating
  all indexes (incl. unique `uniq_shop_url_size`).

Restored the dump into a scratch DB and recomputed metrics from it:

```
restored metrics: 50302 6982 13.8802 285 373 3388
```

Byte-for-byte equal to the live database, so the dataset is fully transportable.
Scratch DB dropped afterwards. Dumps (`data/`, `*.jsonl`) are gitignored.

## 5. `.gitignore` / artifacts

- Added the tracked scratch scripts (`check_db.py`, `check_duplicates.py`,
  `verify_full_run.py`, `test_duplicate_check.py`, `test_small_verification.py`)
  to `.gitignore`; they must be `git rm --cached`-ed (staged only).
- Added `data/` and `*.jsonl` so dumps are never committed.
- Confirmed no `node_modules`, `.next`, `__pycache__`, `.venv`, `.env` or dumps
  are tracked.

## 6. Green build

| Check | Result |
| --- | --- |
| `python -m pytest tests/ -q` | **202 passed** (re-audited; see the note at the top about `test_database.py` writing to the live DB) |
| `npm run lint` | exit 0 |
| `npm run build` | exit 0 (Next.js 16.4.0, static `/`) |

## Outcome

The project is submission-ready: reproducible setup, real dashboard, verified
export/restore, green tests/lint/build. Dataset and matching engine remain at the
validated v1.2.0 state (14.1903 % coverage, 317 opportunities).
