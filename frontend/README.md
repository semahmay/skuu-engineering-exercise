# SKUU Dashboard (frontend)

Next.js (App Router) UI for the SKUU inventory-matching API. It fetches
`GET /api/v1/metrics` and `GET /api/v1/matching/groups` and renders the KPIs,
the per-brand / per-shop splits, and back-in-stock opportunity routes.

## Requirements

- Node.js 20+ (developed on Node 22)
- The backend API running (see the repository root [`README.md`](../README.md))

## Setup

```bash
npm install
cp .env.example .env.local   # optional; defaults to http://localhost:8000
npm run dev                  # http://localhost:3000
```

The API base URL is read from `NEXT_PUBLIC_API_BASE_URL` at build time
(`src/app/page.tsx`, `src/app/RefreshPanel.tsx`); if unset it defaults to
`http://localhost:8000`. The backend must allow the frontend origin via CORS
(`CORS_ORIGINS`).

## Scripts

```bash
npm run dev     # start the dev server
npm run build   # production build
npm start       # serve the production build
npm run lint    # eslint (next/core-web-vitals)
npm test        # node --test unit tests for src/lib/opps.ts and src/lib/refresh.ts
```

## Notes

- The page handles loading, empty-filter and API-error states; when the API is
  unreachable it shows a "Could not reach the API" panel and keeps any
  previously loaded data.
- Matching is computed entirely by the backend — this app only renders the
  response. `src/lib/opps.ts` holds the pure, React-free helpers (with unit
  tests in `scripts/opps.test.ts`).
