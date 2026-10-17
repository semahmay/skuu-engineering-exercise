# Veja Webshop — Discovery Log

**Phase:** 1 (discovery only, no scraper built, no catalog downloaded)
**Target:** https://www.veja-store.com/
**Method:** normal `httpx` + headless Playwright/Chromium, public pages only.
No Cloudflare/CAPTCHA bypass, no stealth, no proxies, no fingerprint spoofing,
no authentication bypass. Plain request clients were not blocked at any point.

---

## 1. Platform identification

The store is **Salesforce Commerce Cloud (SFCC / Demandware)**. Evidence:

- AJAX URLs are served from `/on/demandware.store/Sites-VEJA-UK-Site/en_GB/...`
- Variant query parameters use the `dwvar_<PID>_<attribute>=<value>` convention.
- Sitemap index and per-type sitemaps are the standard SFCC generator output.

`https://www.veja-store.com/` responds `301 -> https://www.veja-store.com/fr_fr/`,
so the **default storefront is France / French / EUR**.

---

## 2. Product discovery

### robots.txt

`https://www.veja-store.com/robots.txt` (200) only disallows analytics/pref URLs:

```
User-agent: *
Disallow: /*__Analytics-Start*
Disallow: /*|*
Disallow: /*pref*1=*
```

Product pages and sitemaps are **not** disallowed. No `Sitemap:` directive is
advertised, but the SFCC sitemap index is reachable.

### Sitemaps

- `sitemap.xml` -> **404** (not used by this site)
- `sitemap_index.xml` -> **200** (the real index)

Per-locale index, e.g. `https://www.veja-store.com/fr_fr/sitemap_index.xml`:

```
sitemap_0-product.xml   42118 URLs
sitemap_1-product.xml   42274 URLs
sitemap_2-product.xml   15809 URLs
sitemap_3..5-image.xml
sitemap_6-category.xml
sitemap_7-content.xml
sitemap_8-pipeline.xml
sitemap_9.xml
```

**Total product URLs found = 100,201.**

### Locale-specific and duplicate URLs

Product URL pattern:

```
https://www.veja-store.com/{lang}_{country}/p/{slug}-{CODE}.html
```

The `fr_fr` product sitemaps are actually a *global "rest of world"* sitemap:
they contain **97 locales** (e.g. `en_al`, `en_dz`, `fr_fr`, `cs_cz`, `da_dk`,
`en_ie`, `en_de`, `en_nl`, ...). Each locale has the **same ~1,033 products**,
only the locale prefix differs.

- **100,201 URLs / 97 locales = 1,033 URLs per locale** (verified: the counts
  divide exactly, 97 * 1033 = 100,201).
- **Unique product codes in the global set = 1,033.**
- `en_gb` is a **separate UK storefront** (`/en_gb/sitemap_index.xml`, one
  product sitemap, 2,504 URLs incl. `en_gb`/`en_im`/`en_gg`, **626 unique
  `en_gb` codes**).

Set comparison (global-1033 vs en_gb-626):

```
overlap 604 | only global 429 | only en_gb 22 | union 1055
```

So regional catalogs differ slightly; there is **no single global catalog**.
Each product is repeated once per locale — these are locale duplicates of the
same SKU, not distinct products.

**Conclusion:** the sitemap contains the full per-storefront catalog and is the
authoritative discovery source. Do not crawl listing pages to enumerate.

### Category / listing pages (secondary)

`/en_gb/sitemap_2-category.xml` -> 396 category URLs (~99 categories x 4 UK
locales, e.g. `women`, `men`, `men-sandals`, `accessories-socks`). A listing page
(`/en_gb/men`) returns ~42 server-rendered `.product-tile` items with a
**"Load more"** control driven by `start` / `sz` parameters. Listing pages are
not needed because the sitemap is complete.

---

## 3. Product page structure

Pages are **server-rendered**; the size/stock DOM is present in the raw HTML
(verified: an `httpx` GET returned 54 size buttons and 22 `disabled` markers
without any JavaScript). JSON-LD is present too, but is **product-level only**.

### JSON-LD (`schema.org/Product`)

```json
{
  "@type": "Product",
  "name": "CAMPO LEATHER FULL STEEL",
  "mpn": "CP0503324",
  "sku": "CP0503324",
  "image": ["https://media.veja-store.com/..."],
  "offers": {
    "priceCurrency": "GBP",
    "price": "87.50",
    "availability": "http://schema.org/InStock"
  }
}
```

- `sku`/`mpn` = product (style) code.
- `offers.availability` is **product-level** and cannot express per-size stock.
- **No `gtin`/`barcode`/`ean`/`upc` is emitted.**

### Colours are separate products

Each colour has its **own product code and URL**, e.g. Campo Leather:

```
FULL-STEEL -> .../campo-leather-full-steel-CP0503324.html
FULL-PARME -> .../campo-leather-full-parme-CP0503323.html
```

The page defaults to the colour named in the URL; colour swatches are
`button[data-type="color"]` with `data-value` (e.g. `FULL-STEEL_03324`) and
`data-display-value` (localized, e.g. `Blue` / `Blanc`). Because colours are
separate codes, **no colour switching is required** during scraping.

---

## 4. Size extraction

Sizes are **buttons**, not radios/selects:

```html
<li class="attributes__item-option">
  <button type="button"
          data-url=".../Product-Variation?dwvar_CP0503324_color=FULL-STEEL_03324&dwvar_CP0503324_size=35-C&pid=CP0503324&quantity=1"
          data-value="35-C"
          data-display-value="2"
          data-type="size"
          class="btn btn-primary swatch size-selector desktop">2</button>
</li>
```

- `data-value` = internal size code (`35-C`, `37.5-C`, `001-C`, `115-C`).
- `data-display-value` = display label (numeric shoe size, `O.S`, `115 CM`,
  or combined `W8 / M7`).
- The list is rendered **three times** per page: `desktop`, `mobile`, and a
  `store-availability` variant. Deduplicate by `data-value`.

---

## 5. Stock extraction (the key question)

**Per-size stock is encoded in the class of the `<li>` that wraps each size
button:**

| `<li>` class | Meaning |
|---|---|
| `attributes__item-option` | in stock |
| `attributes__item-option low-stock` | in stock, low quantity |
| `attributes__item-option disabled sold-out low-stock` | **out of stock** |

Rule used:

```
in_stock = not ("disabled" in li_class or "sold-out" in li_class)
```

Verified live on Campo Leather Full Steel (`en_gb`), which is genuinely mixed:

```
35-C     in      38.5-C   OUT     42.5-C OUT     45-C  OUT
36-C     in      39-C     OUT     43-C   OUT     46-C  OUT
37-C     in      40-C     in      43.5-C OUT     47-C  low
37.5-C   OUT     41-C     low     44-C   OUT     48-C  OUT
38-C     in      42-C     OUT
```

Cross-checked against the SFCC AJAX endpoint (same product):

```
GET /on/demandware.store/Sites-VEJA-UK-Site/en_GB/Product-Variation
    ?dwvar_CP0503324_color=FULL-STEEL_03324
    &dwvar_CP0503324_size=37.5-C
    &pid=CP0503324&quantity=1
-> 200 application/json
   product.id              = "CP0503324A375"
   product.available       = false
   product.availability    = {"messages": ["This item is currently not available"], ...}
   product.price.sales     = {"value": 87.5, "currency": "GBP", "decimalPrice": "87.50"}
-> size 35-C: product.available = true, messages ["In Stock"]
```

DOM and AJAX agree. The AJAX endpoint is therefore available as an
**authoritative fallback**, but the DOM alone is sufficient (one page load per
product, no per-size requests).

> Do not rely on JSON-LD availability: it stayed `InStock` at product level even
> though 11 of 18 sizes were sold out.

---

## 6. Barcode / SKU

- **No GTIN/EAN/UPC/barcode** is exposed anywhere (JSON-LD, DOM, or AJAX).
- JSON-LD `sku`/`mpn` and the URL code = the product/style code (e.g. `CP0503324`).
- The AJAX `product.id` = style code + size suffix (e.g. `CP0503324A350`), i.e. a
  usable per-size variant identifier.
- Recommendation: store `barcode = ""` (not available), keep the style code as
  `url` tail / product identifier, and optionally keep the variant id.

---

## 7. Price / currency

From JSON-LD `offers.price` + `offers.priceCurrency`, or the AJAX
`product.price`:

| Storefront | Currency | Example (Campo Leather Full Steel) |
|---|---|---|
| `en_gb` | GBP | £87.50 (sale), list £125 |
| `fr_fr` | EUR | €125.00 |

Sale price is what JSON-LD reports; the AJAX endpoint exposes both
`price.sales` and `price.list`.

---

## 8. Edge cases

| Case | Found | Detail |
|---|---|---|
| Normal multi-size | yes | Campo Leather, 18 sizes |
| Mixed-stock | yes | Campo Leather Full Steel (7 in/low, 11 out) |
| One Size | yes | Tote Bag -> single size `O.S`, code `001-C` |
| Length-sized accessory | yes | Laces -> `115/125/135/145 CM`, code `115-C` |
| Colour variants | yes | separate product codes (`CP0503324` / `CP0503323`) |
| Fully sold out | **not found** | ~45 products sampled, all had >=1 size in stock |
| Nearly sold out | yes | e.g. `RS0503406` 1 in / 11 out, `VR1820834` 1 in / 12 out |

A fully sold-out product was not found quickly. The store clearly supports the
state (a `Notify me` flow exists: `#mobileSoldOutToggle`, `.sold-out__modal`,
`soldOutModal`, button class `total-sold-out`), so the scraper must handle the
case where **every** `<li>` is `disabled sold-out` (and fall back to JSON-LD
`availability = OutOfStock`).

---

## 9. Problems encountered

- `sitemap.xml` is 404 — must use `sitemap_index.xml`.
- The first stock check looked only at `button` attributes and saw identical
  classes for in/out sizes. The real signal is on the parent `<li>`, not the
  button.
- The product URL contains the colour slug, but the trailing code is the
  product code; the same style code can appear as a different slug per colour
  only via a different code, so no slug->code ambiguity in practice.
- `fr_fr` / `en_ie` returned 404 for the `campo-leather-full-steel` slug (that
  colour is not in those catalogs); must always use sitemap-derived URLs rather
  than constructing them.
- Listing pages load products lazily ("Load more"); not used because the sitemap
  is complete.

---

## 10. Carhartt reuse vs Veja-specific

**Reusable from `backend/app/scrapers/carhartt.py` (unchanged):**

- Browser/context creation, heavy-resource blocking, OneTrust cookie handler.
- Bounded retry per URL, browser auto-rebuild on crash, failure classification,
  progress logging, final summary, `db_counts`.
- Upsert logic (insert new / refresh existing) and the unique
  `(shop, url, size)` index.
- The shared SKU model: `shop, brand, title, colour, size, price, currency,
  barcode, in_stock, url, scraped_at`.

**Veja-specific:**

- Discovery via SFCC `sitemap_index.xml` -> `*-product.xml`, filter by locale
  prefix, dedupe by code.
- DOM selectors: `li.attributes__item-option` + `button[data-type="size"]`;
  stock via `disabled` / `sold-out` classes on the `<li>`.
- JSON-LD mapping: `sku`/`mpn` = product code, `offers.price`/`priceCurrency`.
- One Size handling (`O.S` / `001-C`) and length sizes (`115-C`).
- Colour comes from the URL's product (each colour is its own code).
- `barcode` is always empty (not exposed).
- Optional AJAX `Product-Variation` fallback for authoritative per-size stock.

---

## 11. Recommended scraper strategy

1. **Discover:** GET `/{locale}/sitemap_index.xml`, collect `*-product.xml`
   URLs, parse all `<loc>`, keep only the chosen locale prefix, dedupe by code.
2. **Choose one locale** (decision for Phase 2):
   - `fr_fr` — site default, EUR, **1,033 products**; or
   - an English EUR locale such as `en_ie` — English UI, EUR, ~1,033; or
   - `en_gb` — GBP, **626 products** (UK storefront only).
3. **Per product:** load the page (Playwright for consistency with Carhartt;
   plain `httpx` also works since the DOM is server-rendered). Parse JSON-LD
   (title, code, price, currency) and the size `<li>` list.
4. **Per size:** `size` = `data-display-value`; `size_code` = `data-value`;
   `in_stock` = no `disabled`/`sold-out` in the parent `<li>` class.
5. **Build SKUs:** `shop="veja"`, `brand="Veja"`, `title`, `colour` (from
   JSON-LD/selected swatch), `size`, `price`, `currency`, `barcode=""`,
   `in_stock`, `url`, `scraped_at`.
6. **Persist:** upsert with the existing unique `(shop, url, size)` index.
7. **Robustness:** reuse Carhartt retry/recovery/progress; optionally call
   `Product-Variation` per size as an authoritative fallback when the DOM is
   ambiguous (e.g. unknown class combinations).
8. **Expected size:** ~1,033 products x ~14 sizes ≈ 14k SKUs for `fr_fr`
   (single default colour per URL).

**Do not** start the full run until this strategy is approved.

---

## 12. Artifacts (investigation scripts)

Untracked discovery helpers written under `backend/`:

- `veja_discover_sitemap.py`, `veja_discover_products.py`,
  `veja_discover_unique.py`, `veja_discover_unmatched.py`,
  `veja_discover_compare.py`, `veja_discover_locale.py`,
  `veja_discover_categories.py`
- `veja_discover_page.py`, `veja_discover_stock.py`,
  `veja_discover_extract.py`, `veja_discover_soldout.py`,
  `veja_discover_listing.py`, `veja_discover_nojs.py`,
  `veja_discover_currency.py`, `veja_discover_fr.py`
- Generated: `veja_unique_urls.txt`, `veja_product_variation_sample.json`,
  `veja_page_*.html`

No Carhartt files were modified.
