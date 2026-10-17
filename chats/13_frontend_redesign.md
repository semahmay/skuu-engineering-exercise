# Premium dashboard redesign (frontend only)

Redesigned the Next.js dashboard into a premium SaaS-style interface, adapting
the look and interactions from the `dashboard.html` design reference that was
dropped into the repo root. **No backend code or matching logic changed** — the
UI runs entirely off the existing read-only API.

## What changed

| File | Change |
| --- | --- |
| `frontend/src/app/page.tsx` | Rewritten around the new design, wired to the live API |
| `frontend/src/app/globals.css` | Ported the design system (scoped under `.skuu-app`) |
| `frontend/src/app/layout.tsx` | Inter font + updated metadata |

The old dark, table-only page (four plain KPI cards + two tables) is replaced.

## Data: still 100% backend-driven

No client-side matching, no demo data. The page fetches, in parallel:

- `GET /api/v1/metrics` — headline numbers + per-shop / per-brand breakdowns
- `GET /api/v1/matching/groups?limit=500` — matched groups, turned into
  back-in-stock "routes" (sold-out shop → in-stock shops)

Everything is computed by the frozen v1.2.0 engine on the server. The
`dashboard.html` file-upload / demo-mode path was intentionally **not** ported:
reproducing the matcher in the browser would fork the source of truth.

## Design elements

- **Premium shell** — layered radial gradients + dot grid, glassy cards, Inter.
- **Four KPI cards** with mini-visualisations: animated dot matrix (coverage),
  venn (matched), gauge (coverage), and a "shipping route" motif
  (back-in-stock). Values count up on load and on brand switch.
- **Brand tabs** — All brands / Carhartt WIP / Salomon / Veja. Selecting one
  recomputes the four KPIs from the accurate `by_brand` figures (e.g. Salomon:
  16,243 SKUs · 368 matched · 2.27% · 41 opportunities).
- **Split panels** — by brand and by shop, ranked by coverage, animated bars,
  avatar initials, "Top coverage" crown, per-row back-in-stock count.
- **Back-in-stock explorer** two views:
  - **Space** — 3D coverflow in depth (perspective, holo sheen, orbiting
    floor), navigable by buttons, arrow keys, horizontal wheel and drag.
  - **Grid** — responsive cards with 3D tilt + pointer-tracking shine,
    "Hot route" badges for routes with 3+ destinations, and pagination.
- **Search + sold-out-shop filter** over the routes.
- **States** — spinner while the (recursive) matching runs, a live "engine
  v1.2.0" banner, and an error card if the API is unreachable.

## Data-shape handling

The API returns scraper slugs; the UI maps them to friendly labels so the
dashboard reads like a product, not a database:

- shops: `carhartt_wip → Carhartt WIP`, `afew → AFEW Store`,
  `nugnes1920 → Nugnes 1920`, …
- brands: `carhartt wip → Carhartt WIP`, `veja → Veja`, `salomon → Salomon`

Filtering still uses the raw slug values under the hood.

## Verification

- `npm run lint` → clean; `npm run build` → success (Next 16.4.0, static `/`).
- Headless Chromium (Playwright) against the live API:
  - loader visible while fetching
  - KPIs `50,302 · 6,982 · 13.88% · 285`
  - tabs `All brands · Carhartt WIP · Salomon · Veja`
  - Salomon tab → `16,243 · 368 · 2.27% · 41`
  - space view: 60 cards laid out, centred active card, `1 / 60` (224 routes)
  - grid view: 24 cards with sold-out / in-stock nodes
  - API aborted → "Could not reach the API" card shown
  - no console errors
