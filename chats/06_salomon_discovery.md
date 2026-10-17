# Salomon Official Store — Discovery Report (Phase 1)

**Target:** https://www.salomon.com/  ·  **Candidate locale:** `en-nl` (English, EUR) / `en-gb` / `en-us`
**Platform:** Adobe Commerce / Magento (PWA front end served via Vercel)
**Bot protection:** PerimeterX / HUMAN Security (`appId PX2bubKzBv`) on all storefront + API routes
**Status:** Catalog discovery **feasible**; product-page extraction **NOT feasible without bypassing access controls**

> Scope of this document: investigation only. No scraper was implemented.
> No Carhartt/Veja code, DB schema, or matching engine was touched.

---

## 1. Method & evidence

All probes were plain `httpx` (HTTP/2) and a plain Playwright Chromium (no stealth,
no proxies, no fingerprint spoofing), with delays between requests. Public pages only.

| Probe | Result |
|---|---|
| `GET /robots.txt` | 200 (served) |
| `GET /sitemap.xml` | 200 — custom sitemap index |
| `GET /en-us`, `/en-gb`, `/` (httpx) | **403** "Access to this page has been denied" |
| `GET /en-us/product/...` (httpx) | **403** |
| `GET /en-us/product/...` (Playwright Chromium) | **403**, loads PerimeterX `captcha.js` + `px-cloud.net` |
| `POST /graphql` | **403** (PX JSON challenge) |
| `GET /rest/V1/products` | **403** (PX) |
| `GET /api/sitemap/products?locale=en-us` | **200** (allow-listed, unprotected) |

The 403 body is a PerimeterX challenge document (`appId`, `jsClientSrc`,
`hostUrl`, `blockScript .../captcha.js`) and sets a `_pxhd` cookie. A Chromium
navigation receives the same 403 and loads PX scripts — i.e. a CAPTCHA/bot
gate, not a transient rate limit (blocked on the very first request too).

---

## 2. Catalog discovery (feasible)

`robots.txt` is Magento-style (Magento path disallows, `catalogsearch`,
`catalog/product/view`) and has **no `Sitemap:` directive**, but a custom index
is exposed at `https://www.salomon.com/sitemap.xml`:

```
/api/sitemap/default      -> per-locale homepage sitemap (1,580 URLs, 19 locales)
/api/sitemap/products?locale=XX   <-- per-locale PRODUCT sitemap
/api/sitemap/categories?locale=XX
/api/sitemap/lp           -> landing pages
/api/sitemap/athletes     -> athlete profiles
/api/sitemap/sg           -> stories/guides (training plans, articles)
```

Product URL shape (Magento + PWA):

```
https://www.salomon.com/{locale}/product/<slug>-<stylecode>
e.g. https://www.salomon.com/en-us/product/xa-pro-3d-lg5239
```

- The trailing token (`lg5239`, `li1400`, `lc6507`) is the **Salomon style/variant
  code** – a useful cross-store matching key.
- Each `<url>` block has `<loc>`, `<lastmod>` and `<xhtml:link hreflang>`
  cross-locale alternates. **No price, currency, stock, GTIN/EAN/UPC.**

### Product counts (English locales)

| Locale | Products | Inferred currency |
|---|---:|---|
| `en-cz` | 1,261 | CZK |
| `en-gb` | 1,238 | GBP |
| `en-no` | 1,238 | NOK |
| `en-nl` | 1,237 | EUR |
| `en-dk` | 1,237 | DKK |
| `en-fi` | 1,235 | EUR |
| `en-us` | 824 | USD |
| `en-ca` | 773 | CAD |

There is **no `en-ie` and no global `en-int`** in the sitemap index (`/en-int`
redirects to `/en-us`). The English locales are separate storefronts per country.

### Duplicate URLs

Sitemap `<loc>` values are **unique per locale** (824/824, 1238/1238). Colours are
distinct product URLs (distinct trailing codes), so de-duplication is by code, as
with Veja. No page-level pagination exists in the sitemap approach (the full list
is delivered in one request).

---

## 2b. Recommended storefront

- **Primary: `en-nl`** — English UI, **EUR**, full catalog (1,237) — most
  consistent with the project's existing EU/EUR orientation.
- **Alternatives:** `en-gb` (GBP, 1,238), `en-us` (USD, 824).
- Currency is **inferred from the locale/region**; it could **not** be verified
  live because product pages are blocked (see §3).

---

## 3. Product data — blocked

Required fields and whether they can be obtained:

| Field | Source | Obtainable? |
|---|---|---|
| Product title | product page / JSON-LD | **No** (403) |
| Product URL | sitemap | **Yes** |
| Colour | product page (swatch) | **No** (403) |
| Sizes | product page | **No** (403) |
| Price + currency | product page / JSON-LD | **No** (403) |
| Barcode/GTIN/EAN/UPC | product page / JSON-LD | **No** (403) |
| Per-size stock | product page | **No** (403) |
| Style/variant code | sitemap URL | **Yes** |

Everything except the sitemap data is behind PerimeterX. The standard Magento
data channels are all protected too:

- **GraphQL** `POST /graphql` → 403 PX
- **REST** `/rest/V1/products` → 403 PX
- Category pages `/en-us/c/...` → 403 PX

No unprotected JSON/feed endpoint was found (`/api/products`, `/api/product`,
`/api/search`, `/media/feeds/...`, `/product-export` → 404/403; `/api/sitemap/*`
is the only allow-listed API).

Because individual per-size availability can only be read from the product page
(which is blocked), **we cannot validate stock at all** — and we must not infer
size stock from a product-level field even if one were obtainable.

---

## 4. Live validation

Attempted a representative sample; **none** returned page content:

| URL | Expected variant | Result |
|---|---|---|
| `/en-us/product/xa-pro-3d-lg5239` | shoe, mixed sizes | 403 PX (httpx and Chromium) |
| `/en-gb/product/xa-pro-3d-lg5239` | en-GB variant | 403 PX |
| `/en-us/c/men/shoes` | category | 403 PX |
| `.../product/xa-pro-3d-lg5239.html` | legacy path | 403 PX |
| apex `https://salomon.com/...` | non-www | 403 PX |

**Uncertainty: total.** No size/price/colour/stock value could be compared against
a page, because no page content was served to an automated client.

---

## 4b. Integration approach (for when access exists)

Simplest approach consistent with the existing architecture:

- Add `backend/app/scrapers/salomon.py` mirroring `veja.py`.
- Constants: `SHOP_ID="salomon"`, `BRAND="Salomon"`, `LOCALE="en-nl"`,
  `BASE_URL="https://www.salomon.com"`, delay/retry constants.
- Discovery: fetch `/api/sitemap/products?locale=en-nl`, filter
  `/en-nl/product/`, extract trailing style code, de-duplicate.
- Parse each product (browser or HTTP), read JSON-LD `Product` and the size
  selector, map `size -> in_stock`.
- Persist via the existing `upsert_skus` + unique `(shop, url, size)` index.
- Reuse `classify_failure`, `accept_cookies`, retry/recovery from `carhartt.py`
  (do not modify it).

**However**, the exact DOM selectors, structured-data shape, and stock encoding
**cannot be documented yet** because no product page is reachable. Writing a
parser now would be guesswork.

---

## 4. Stock verification

Not possible in Phase 1. No product page could be loaded, so:

- The representation of sold-out vs available sizes is **unknown**.
- Whether JSON-LD / embedded JSON exposes per-size availability is **unknown**.
- Whether a product-level availability field is reliable is **unknown**.

The rule "do not infer stock from product-level availability if individual size
availability can be checked" cannot even be evaluated until page access exists.

---

## 5. Project integration review (done)

- `backend/app/scrapers/carhartt.py` — provides `upsert_skus`,
  `classify_failure`, `accept_cookies`, retry/recovery, progress printing.
  **Unchanged.**
- `backend/app/scrapers/veja.py` — the closest template for a future
  `salomon.py` (sitemap discovery + DOM/JSON-LD parse + upsert).
- `backend/app/models/sku.py` — SKU schema (`shop, brand, title, colour, size,
  price, currency, barcode?, in_stock, url, scraped_at`).
- `backend/app/database/repository.py` — unique index `uniq_shop_url_size`
  on `(shop, url, size)`.
- A future `salomon.py` maps onto these **without any schema or matching change**.

---

## 6. Feasibility conclusion

| Aspect | Verdict |
|---|---|
| Full catalog **URL** discovery | **Feasible** (unprotected sitemap API) |
| Product **title/price/colour/sizes/stock/barcode** | **Blocked** by PerimeterX |
| Feasible to build a compliant scraper **now** | **No** |

Salomon's DTC site actively blocks automated access with a PerimeterX
CAPTCHA challenge. Getting product data would require circumventing that control
(stealth automation, CAPTCHA solving, proxy/fingerprint evasion), which the
project rules explicitly forbid. The catalog can be enumerated, but the required
product/size/stock data cannot be collected legitimately at this time.

---

## 7. Recommended next steps (pick one)

1. **Official/partner product feed** — use a merchant/affiliate feed (e.g.
   Google Merchant Center or the affiliate network Salomon uses) if access can be
   provided. Legitimate and gives price/stock/GTIN.
2. **Scrape an authorized retailer** that sells Salomon (the planned
   "retailers" phase) instead of the brand DTC site.
3. **Re-scope** the Salomon target to a property that permits automation.
4. **Pause Salomon** and proceed to the next brand/phase.

Pending a decision, **no `salomon.py` will be implemented**.

---

## Appendix — discovery artifacts (gitignored)

- `backend/salomon_discover_root.py` — storefront/robots probes
- `backend/salomon_discover_probe.py` — robots + sitemap + browser test
- `backend/salomon_discover_sitemap.py` / `_sitemap2.py` — sitemap index/locale
- `backend/salomon_discover_api.py` / `_api2.py` — GraphQL/REST/feed probes
- `backend/salomon_discover_catalog.py` — product counts per locale, `sg` analysis
- `backend/salomon_discover_xml.py` — sitemap structure
- `backend/salomon_discover_browser2.py` — Playwright product-page test
- `backend/salomon_en_us_products.xml`, `salomon_en_gb_products.xml`, etc.
- `backend/salomon_block_page.html` — captured PerimeterX block page
