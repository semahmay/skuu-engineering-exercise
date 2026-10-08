# Carhartt WIP Discovery — 2026-10-07

## Summary
- **Target**: https://carhartt-wip.com (redirects to locale-specific, e.g., https://www.carhartt-wip.com/en-de)
- **Accessibility**: ✅ Fully accessible, no Cloudflare/bot protection
- **Cookie consent**: Simple "Accept all" button — easily handled
- **JavaScript**: Required for size selection interaction; JSON-LD available in HTML
- **Sitemap**: ✅ Available at `/sitemap.xml` → locale sitemaps → `/en-de/product/sitemap.xml` (946 products)

---

## 1. Website Access & Security
- **Homepage**: `https://www.carhartt-wip.com/en-de` returns 200 OK
- **Cloudflare**: ❌ Not detected
- **Bot protection**: ❌ None detected
- **Cookie banner**: ✅ Present, single "Accept all" button (`button:has-text("Accept all")`)
- **Rate limits**: Not observed during discovery; polite delays recommended

---

## 2. Product Catalog Structure

### URL Patterns
- **Locale homepage**: `https://www.carhartt-wip.com/{locale}` (e.g., `en-de`, `en-gb`, `de-de`, `en-fr`, `fr-fr`, `it-it`, `en-it`, `es-es`, `en-es`, `pt-pt`, `en-pt`, `nl-nl`, `en-nl`)
- **Category pages**: `https://www.carhartt-wip.com/{locale}/c/{category}` (e.g., `/c/men`, `/c/women`, `/c/accessories`, `/c/new-arrivals`)
- **Product pages**: `https://www.carhartt-wip.com/{locale}/p/{product-handle}-{numeric-id}`
  - Example: `https://www.carhartt-wip.com/en-de/p/orman-bomber-raven-heavy-enzyme-wash-697`

### Sitemap Hierarchy
```
https://www.carhartt-wip.com/sitemap.xml
  └── 38 locale sitemaps (e.g., /en-de/sitemap.xml)
        └── /en-de/product/sitemap.xml   ← 946 product URLs
        └── /en-de/category/sitemap.xml
        └── /en-de/cms/sitemap.xml
```
- Each `<url>` entry has `<loc>` and `<lastmod>` (date only, e.g., `2026-10-07`)

### Product Count
- **en-de locale**: 946 products in product sitemap
- Other locales likely similar counts

---

## 3. Product Detail Page — Data Sources

### A. JSON-LD (schema.org/Product) — PRIMARY SOURCE
Present in `<script type="application/ld+json">` on every product page. Contains:

```json
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Orman Bomber",
  "image": ["https://cdn.media.amplience.net/i/carhartt_wip/I037012_3T6_4G-ST-01?$google_struc_main_st$", ...],
  "sku": "I037012_15",
  "size": ["XS", "S", "M", "L", "XL", "XXL"],
  "material": "100% Nylon Twill, 5.2 oz",
  "color": "Raven",
  "brand": { "@type": "Brand", "name": "Carhartt WIP" },
  "offers": {
    "@type": "Offer",
    "price": 299,
    "priceCurrency": "EUR",
    "availability": "https://schema.org/InStock",
    "url": "https://www.carhartt-wip.com/p/orman-bomber-raven-heavy-enzyme-wash-697",
    "itemCondition": "https://schema.org/NewCondition"
  }
}
```

**Fields extracted:**
| SKU Field | JSON-LD Path | Notes |
|-----------|--------------|-------|
| `shop` | constant | `"carhartt_wip"` |
| `brand` | `brand.name` | `"Carhartt WIP"` |
| `title` | `name` | Product name |
| `colour` | `color` | Single color string |
| `size` | `size[]` | Array of size strings |
| `price` | `offers.price` | Numeric |
| `currency` | `offers.priceCurrency` | `"EUR"` |
| `barcode` | `sku` | Carhartt internal SKU (e.g., `I037012_15`) |
| `in_stock` | `offers.availability` | Boolean: `InStock` → true |
| `url` | `offers.url` | Canonical product URL |
| `scraped_at` | — | Set at scrape time |

**Limitation**: JSON-LD shows **one aggregated Offer** with all sizes and `InStock`. Does **not** indicate per-size stock.

### B. Rendered HTML (requires JS) — FOR PER-SIZE STOCK
- Size options rendered as radio buttons (`input[type="radio"][name="id"]` with `value` = variant ID)
- Each size has a corresponding variant ID
- Clicking a size may update price/availability
- "Add to cart" button (`button.product-form__submit`) disabled when size OOS
- Need to iterate sizes and check button state per variant

### C. Embedded Next.js Data (in `<script id="__NEXT_DATA__">`)
- Large JSON blob with full product data including variants
- Could be parsed for variant-level stock without clicking
- More complex to extract; JSON-LD + HTML interaction is simpler

---

## 4. Size/Variant Representation

### JSON-LD
- Single `offers` object with `size` array listing all sizes
- Single `availability` = `InStock` (aggregated)

### HTML (JS-rendered)
- Radio group: `input[type="radio"][name="id"]` — each has unique `value` (variant ID)
- Labels show size text (XS, S, M, L, XL, XXL)
- Selected size updates "Add to cart" button state
- OOS sizes: button becomes disabled OR size label gets strikethrough/disabled class

### Variant ID Mapping
- Each size = one variant = one SKU record
- Variant ID appears in radio `value` and possibly in JSON-LD `sku` suffix (e.g., `I037012_15` → `_15` might map to a size)

---

## 5. Stock / Availability Representation

| Source | Granularity | Reliability |
|--------|-------------|-------------|
| JSON-LD `offers.availability` | Product-level (all sizes) | Shows `InStock` if **any** size available |
| HTML "Add to cart" button | Per-size (after selection) | Accurate per variant |
| Size radio button state | Per-size | Disabled/hidden if OOS |

**Strategy**: Use JSON-LD for product metadata (title, color, price, all sizes). Then for each size, select via radio and check if "Add to cart" is enabled → `in_stock` per SKU.

---

## 6. Pagination / Discovery

### Recommended: Sitemap-based Discovery
- Fetch `https://www.carhartt-wip.com/en-de/product/sitemap.xml`
- Parse all `<loc>` URLs → 946 product URLs
- No pagination logic needed
- Respect `<lastmod>` for incremental scrapes

### Alternative: Category Pages
- `/en-de/c/men`, `/en-de/c/women`, `/en-de/c/accessories`
- Infinite scroll or pagination
- More fragile; sitemap is authoritative

---

## 7. Proposed Scraper Architecture

```
CarharttScraper
├── discover_products() → List[str] (from sitemap)
├── scrape_product(url) → List[SKU]
│   ├── fetch HTML (with JS, block images/fonts)
│   ├── accept cookies
│   ├── parse JSON-LD → base product data + all sizes
│   ├── for each size in JSON-LD.size[]:
│   │   ├── select size via radio button
│   │   ├── check "Add to cart" button disabled state
│   │   ├── build SKU with size-specific in_stock
│   │   └── collect SKU
│   └── return List[SKU]
└── run(max_products=5) → insert SKUs via repository
```

### Key Implementation Details
- **Playwright**: Chromium, headless, block `image`, `font`, `stylesheet`, `media`
- **Cookie handling**: Click "Accept all" on first page load
- **Delays**: 1-2s between product pages; 500ms between size selections
- **Error handling**: Try/except per product; log and continue
- **Deduplication**: Use `find_sku_by_shop_and_url` before insert
- **Logging**: Structured logs per product + size

---

## 8. Engineering Decisions

| Decision | Rationale |
|----------|-----------|
| Use sitemap for discovery | Authoritative, no pagination logic, supports incremental via `lastmod` |
| JSON-LD for base metadata | Stable, no JS required for core fields, structured |
| JS + radio click for per-size stock | Only reliable way to get per-variant availability |
| Block heavy resources | Speed, reduce crash risk (observed crashes with full resources) |
| One SKU per product+size | Matches SKU model definition |
| Dedupe by `shop` + `url` | Product URL is stable unique key per colorway |

---

## 9. Limitations & Risks

1. **Per-size stock requires JS interaction** — slower, more fragile
2. **JSON-LD `sku` is product-level** — variant SKU mapping not explicit; may need to derive from radio `value`
3. **Single locale (en-de)** — other locales need separate runs or parameterization
4. **No API** — all scraping via HTML/JSON-LD
5. **Site changes** — selectors may break; monitor JSON-LD structure
6. **946 products × ~6 sizes = ~5,600 SKUs** — full scrape takes ~20-30 min with delays

---

## 10. Next Steps (Step 4B)
1. Implement `backend/app/scrapers/carhartt.py` with:
   - `discover_product_urls(limit=N)` from sitemap
   - `scrape_product_page(url)` → `List[SKU]` using JSON-LD + JS size iteration
   - `run(limit=5)` for initial verification
2. Add tests for:
   - Sitemap parsing
   - JSON-LD parsing
   - SKU construction
   - MongoDB insertion (integration test)
3. Document in `chats/01_carhartt_scraper.md`