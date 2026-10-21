"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
} from "react";

import { buildOpps, type Group, type Opp } from "@/lib/opps";
import RefreshPanel from "./RefreshPanel";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

// Hard ceiling for a dashboard request. The backend builds the matching
// snapshot once (a full 62k-row scan plus clustering) and caches it, so a warm
// request is fast; the first request after a cold start or a refresh is the
// slow one. Without a ceiling the loader could spin forever with no explanation.
const REQUEST_TIMEOUT_MS = 120_000;

async function fetchJson<T>(path: string): Promise<T> {
  const controller = new AbortController();
  const startedAt = Date.now();
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!res.ok) {
      let detail = "";
      try {
        const body = (await res.json()) as { detail?: unknown };
        if (body?.detail) detail = `: ${String(body.detail)}`;
      } catch {
        // Non-JSON error body: the status code is the useful part.
      }
      throw new Error(`${path} responded ${res.status}${detail}`);
    }
    return (await res.json()) as T;
  } catch (e) {
    if (controller.signal.aborted) {
      const secs = Math.round((Date.now() - startedAt) / 1000);
      throw new Error(
        `${path} timed out after ${secs}s. The backend may still be computing — retry in a moment.`,
      );
    }
    throw e;
  } finally {
    window.clearTimeout(timer);
  }
}

/* ----------------------------- API types ----------------------------- */

type ShopMetrics = {
  total_skus: number;
  matched_skus: number;
  coverage_pct: number;
  in_stock: number;
  out_of_stock: number;
  back_in_stock_opportunities: number;
};

type BrandMetrics = {
  total_skus: number;
  matched_skus: number;
  coverage_pct: number;
  back_in_stock_opportunities: number;
};

type Metrics = {
  total_skus: number;
  matched_skus: number;
  coverage_pct: number;
  back_in_stock_opportunities: number;
  back_in_stock_skus: number;
  matched_groups: number;
  shops: string[];
  by_shop: Record<string, ShopMetrics>;
  by_brand: Record<string, BrandMetrics>;
  algorithm_version: string;
  generated_at?: string;
  cache?: {
    hit: boolean;
    age_seconds: number;
    build_ms: number;
    total_skus: number;
  };
};

type SplitStat = {
  name: string;
  kind: "brand" | "shop";
  total: number;
  matched: number;
  back: number;
  pct: number;
};

/* ----------------------------- helpers ----------------------------- */

const fmt = (n: number) => Math.round(n).toLocaleString("en-US");
const norm = (x: unknown) =>
  String(x ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();

function hueOf(name: string) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}

function initials(name: string) {
  const w = String(name)
    .replace(/\(.*?\)/g, "")
    .trim()
    .split(/\s+/);
  return (w.length > 1 ? w[0][0] + w[1][0] : w[0].slice(0, 2)).toUpperCase();
}

function colourHex(name: string) {
  const n = norm(name);
  if (n.includes("black")) return "#1a1a1a";
  if (n.includes("white") || n.includes("natural") || n.includes("vanilla"))
    return "#f2efe6";
  if (n.includes("brown")) return "#7a5230";
  if (n.includes("wax")) return "#c9b27c";
  return `hsl(${hueOf(name)} 35% 55%)`;
}

/* Friendly labels for the scraper slugs returned by the API. */
const SHOP_LABELS: Record<string, string> = {
  afew: "afew",
  asphaltgold: "asphaltgold",
  beamhill: "beamhill",
  biffi: "biffi",
  caliroots: "caliroots",
  carhartt_wip: "Carhartt WIP (brand store)",
  footway: "footway",
  nugnes1920: "nugnes1920",
  sneakersnstuff: "sneakersnstuff",
  salomon: "Salomon (brand store)",
  ssense: "SSENSE",
  veja: "Veja (brand store)",
  xtreme: "xtreme",
};

// The three direct-to-consumer brand stores. They are shops, but not the
// third-party "retailers" the exercise counts, and their labels must not be
// confused with the brand names they sell.
const BRAND_STORE_IDS = new Set(["carhartt_wip", "veja", "salomon"]);
const retailerCount = (shops: string[]) =>
  shops.filter((s) => !BRAND_STORE_IDS.has(s)).length;

const BRAND_LABELS: Record<string, string> = {
  "carhartt wip": "Carhartt WIP",
  veja: "Veja",
  salomon: "Salomon",
};

function titleize(s: string) {
  return s
    .split(/[\s_]+/)
    .filter(Boolean)
    .map((w) => w[0].toUpperCase() + w.slice(1))
    .join(" ");
}

const prettyShop = (s: string) => SHOP_LABELS[s] ?? titleize(s);
const prettyBrand = (b: string) => BRAND_LABELS[b] ?? titleize(b);

function toSplit(
  entries: [string, Record<string, number>][],
  label: (name: string) => string,
  kind: "brand" | "shop",
): SplitStat[] {
  return entries
    .map(([name, d]) => {
      const total = d.total_skus ?? 0;
      const matched = d.matched_skus ?? 0;
      return {
        name: label(name),
        kind,
        total,
        matched,
        back: d.back_in_stock_opportunities ?? 0,
        pct: total ? (matched / total) * 100 : 0,
      };
    })
    .sort((a, b) => b.pct - a.pct);
}

/* ----------------------------- count-up hook ----------------------------- */

function useCountUp(target: number, decimals = 0) {
  const [val, setVal] = useState(0);
  const fromRef = useRef(0);

  useEffect(() => {
    const from = fromRef.current;
    const start = performance.now();
    const dur = 900;
    let raf = 0;
    const step = (now: number) => {
      const p = Math.min(1, (now - start) / dur);
      const e = 1 - Math.pow(1 - p, 3);
      const v = from + (target - from) * e;
      fromRef.current = v;
      setVal(v);
      if (p < 1) raf = requestAnimationFrame(step);
      else {
        fromRef.current = target;
        setVal(target);
      }
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target]);

  return decimals ? val.toFixed(decimals) : Math.round(val).toLocaleString("en-US");
}

/* ----------------------------- small visuals ----------------------------- */

function Avatar({ name }: { name: string }) {
  const h = hueOf(name);
  return (
    <span
      className="av"
      style={{ background: `hsl(${h} 70% 92%)`, color: `hsl(${h} 55% 28%)` }}
    >
      {initials(name)}
    </span>
  );
}

function DotMatrix({ coverage }: { coverage: number }) {
  const on = Math.round(coverage);
  return (
    <div className="dots" aria-hidden="true">
      {Array.from({ length: 100 }, (_, i) => (
        <i
          key={i}
          className={i < on ? "on" : ""}
          style={{ animationDelay: `${i * 6}ms` }}
        />
      ))}
    </div>
  );
}

function Gauge({ coverage }: { coverage: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const p = Math.max(0, Math.min(100, coverage));
    ref.current?.style.setProperty("--p", p.toFixed(1));
  }, [coverage]);
  return (
    <div className="gauge" ref={ref} aria-hidden="true">
      <span>{coverage.toFixed(1)}% covered</span>
    </div>
  );
}

function SplitRow({ s, best }: { s: SplitStat; best: boolean }) {
  const [w, setW] = useState(0);
  useEffect(() => {
    const id = requestAnimationFrame(() => setW(s.pct));
    return () => cancelAnimationFrame(id);
  }, [s.pct]);
  return (
    <div className="srow">
      <Avatar name={s.name} />
      <div>
        <div className="sname">
          {s.name}
          <span
            style={{
              marginLeft: 6,
              fontSize: ".62em",
              letterSpacing: ".08em",
              textTransform: "uppercase",
              opacity: 0.45,
            }}
          >
            {s.kind}
          </span>
          {best && <span className="crown">★ Top coverage</span>}
        </div>
        <div className="strack">
          <i style={{ width: `${w}%` }} />
        </div>
      </div>
      <div className="sstat">
        <b>{s.pct.toFixed(0)}%</b>
        {fmt(s.matched)} / {fmt(s.total)}
        <br />
        <span className="sback">{fmt(s.back)} back</span>
      </div>
    </div>
  );
}

/* ----------------------------- route card ----------------------------- */

function RouteCard({
  o,
  tilt,
  active,
  delay,
  onClick,
}: {
  o: Opp;
  tilt: boolean;
  active?: boolean;
  delay?: number;
  onClick?: () => void;
}) {
  const style: CSSProperties | undefined =
    delay === undefined ? undefined : { animationDelay: `${delay}ms` };

  const onMove = (e: ReactMouseEvent<HTMLElement>) => {
    if (!tilt) return;
    const c = e.currentTarget;
    const r = c.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    c.classList.add("tilt");
    c.style.setProperty("--ry", `${((x - 0.5) * 18).toFixed(2)}deg`);
    c.style.setProperty("--rx", `${((0.5 - y) * 16).toFixed(2)}deg`);
    c.style.setProperty("--mx", `${(x * 100).toFixed(1)}%`);
    c.style.setProperty("--my", `${(y * 100).toFixed(1)}%`);
  };

  const onLeave = (e: ReactMouseEvent<HTMLElement>) => {
    if (!tilt) return;
    const c = e.currentTarget;
    c.classList.remove("tilt");
    c.style.setProperty("--ry", "0deg");
    c.style.setProperty("--rx", "0deg");
  };

  const alts = o.alts.slice(0, 3);
  const extra = o.alts.length - alts.length;

  return (
    <article
      className={`rc${active ? " active" : ""}`}
      style={style}
      onClick={onClick}
      onMouseMove={onMove}
      onMouseLeave={onLeave}
    >
      {o.alts.length >= 3 && <span className="hot">🔥 Hot route</span>}
      <div className="rc-top">
        <div>
          <div className="brand">{prettyBrand(o.brand)}</div>
          <div className="ttl">{o.title}</div>
          <div className="meta">
            <span className="dot" style={{ background: colourHex(o.colour) }} />
            {o.colour || "—"}
          </div>
        </div>
        <div className="size">
          <small>SIZE</small>
          {o.size}
        </div>
      </div>
      <div className="route">
        <div className="node out">
          <Avatar name={prettyShop(o.shop)} />
          <span>
            <small>Sold out</small>
            {prettyShop(o.shop)}
          </span>
        </div>
        <div className="line" />
        <div className="dests">
          {alts.map((a) => (
            <div className="node in" key={`${o.id}-${a}`}>
              <Avatar name={prettyShop(a)} />
              <span>
                <small>In stock</small>
                {prettyShop(a)}
              </span>
            </div>
          ))}
          {extra > 0 && <div className="meta">+{extra} more</div>}
        </div>
      </div>
      <div className="rc-foot">
        <span className="tag">Sale saved</span>
        <span>
          {o.alts.length} shop{o.alts.length > 1 ? "s" : ""} can ship
          {o.price != null && o.price !== "" ? (
            <>
              {" · "}
              <span className="price">
                {o.currency ? `${o.currency} ` : ""}
                {o.price}
              </span>
            </>
          ) : null}
        </span>
      </div>
    </article>
  );
}

/* ----------------------------- page ----------------------------- */

export default function Home() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [groups, setGroups] = useState<Group[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [groupsError, setGroupsError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);

  const [brand, setBrand] = useState("");
  const [q, setQ] = useState("");
  const [fShop, setFShop] = useState("");
  const [mode, setMode] = useState<"space" | "grid">("space");
  const [active, setActive] = useState(0);
  const [shown, setShown] = useState(24);
  const [refreshOpen, setRefreshOpen] = useState(false);

  const spaceRef = useRef<HTMLDivElement>(null);
  const ringRef = useRef<HTMLDivElement>(null);
  const movedRef = useRef(false);
  const wheelLockRef = useRef(0);

  const fetchData = useCallback(async () => {
    // Both endpoints share one cached snapshot on the backend, so requesting
    // them together costs one computation, not two. Groups are optional: a
    // failure there must not hide the metrics that did load.
    const [mRes, gRes] = await Promise.allSettled([
      fetchJson<Metrics>("/api/v1/metrics"),
      fetchJson<{ groups?: Group[] }>("/api/v1/matching/groups"),
    ]);
    if (mRes.status === "rejected") throw mRes.reason;
    return {
      m: mRes.value,
      g: gRes.status === "fulfilled" ? gRes.value : { groups: [] },
      groupsError:
        gRes.status === "rejected"
          ? gRes.reason instanceof Error
            ? gRes.reason.message
            : "failed to load cross-shop groups"
          : null,
    };
  }, []);

  const applyData = useCallback(
    (m: Metrics, g: { groups?: Group[] }, gErr: string | null) => {
      setMetrics(m);
      setUpdatedAt(m.generated_at ?? new Date().toISOString());
      setGroups(g.groups ?? []);
      setError(null);
      setGroupsError(gErr);
      setLoading(false);
    },
    [],
  );

  const fail = useCallback((e: unknown) => {
    const msg = e instanceof Error ? e.message : "Failed to load dashboard";
    setError(msg);
    setLoading(false);
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchData()
      .then(({ m, g, groupsError: gErr }) => {
        if (!cancelled) applyData(m, g, gErr);
      })
      .catch((e) => {
        if (!cancelled) fail(e);
      });
    return () => {
      cancelled = true;
    };
  }, [fetchData, applyData, fail]);

  const refresh = () => {
    setLoading(true);
    setError(null);
    setGroupsError(null);
    fetchData()
      .then(({ m, g, groupsError: gErr }) => applyData(m, g, gErr))
      .catch(fail);
  };

  const opps = useMemo(() => (groups ? buildOpps(groups) : []), [groups]);

  const list = useMemo(() => {
    const qq = norm(q);
    return opps
      .filter(
        (o) =>
          (!brand || o.brand === brand) &&
          (!fShop || o.shop === fShop) &&
          (!qq || norm(`${o.title} ${o.size} ${o.colour}`).includes(qq)),
      )
      .sort((a, b) => b.alts.length - a.alts.length);
  }, [opps, brand, fShop, q]);

  const spaceList = useMemo(() => list.slice(0, 60), [list]);
  const gridList = useMemo(() => list.slice(0, shown), [list, shown]);
  const shopOptions = useMemo(
    () =>
      [...new Set(opps.map((o) => o.shop))].sort((a, b) =>
        prettyShop(a).localeCompare(prettyShop(b)),
      ),
    [opps],
  );

  const activeEff = Math.min(active, Math.max(0, spaceList.length - 1));

  const changeBrand = (b: string) => {
    setBrand(b);
    setActive(0);
    setShown(24);
  };
  const changeQ = (v: string) => {
    setQ(v);
    setActive(0);
    setShown(24);
  };
  const changeShop = (v: string) => {
    setFShop(v);
    setActive(0);
    setShown(24);
  };

  const layoutSpace = useCallback(() => {
    const ring = ringRef.current;
    const space = spaceRef.current;
    if (!ring || !space) return;
    const kids = Array.from(ring.children) as HTMLElement[];
    if (!kids.length || !kids[0].classList.contains("rc")) return;
    const w = space.clientWidth;
    const gap = Math.min(250, w / 3.1);
    kids.forEach((c, i) => {
      const d = i - activeEff;
      const a = Math.abs(d);
      const s = Math.sign(d);
      if (a > 3) {
        c.style.opacity = "0";
        c.style.pointerEvents = "none";
        c.style.zIndex = "0";
        c.style.transform = `translateX(${s * gap * 3.5}px) translateZ(-600px)`;
        c.style.filter = "none";
        return;
      }
      c.style.opacity = (1 - a * 0.2).toFixed(2);
      c.style.pointerEvents = "auto";
      c.style.zIndex = String(100 - a);
      c.style.transform = `translateX(${d * gap + s * (a ? 70 : 0)}px) translateZ(${
        -a * 190
      }px) rotateY(${-s * Math.min(a * 34, 62)}deg)`;
      c.style.filter = a ? `blur(${(a * 0.7).toFixed(1)}px) saturate(.85)` : "none";
    });
  }, [activeEff]);

  useEffect(() => {
    if (mode !== "space") return;
    const id = requestAnimationFrame(layoutSpace);
    return () => cancelAnimationFrame(id);
  }, [mode, layoutSpace, spaceList.length]);

  useEffect(() => {
    const onResize = () => {
      if (mode === "space") layoutSpace();
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [mode, layoutSpace]);

  useEffect(() => {
    if (mode !== "space") return;
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName ?? "";
      if (/INPUT|SELECT|TEXTAREA/.test(tag)) return;
      if (e.key === "ArrowRight") setActive((a) => Math.min(a + 1, spaceList.length - 1));
      else if (e.key === "ArrowLeft") setActive((a) => Math.max(a - 1, 0));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, spaceList.length]);

  useEffect(() => {
    if (mode !== "space") return;
    const space = spaceRef.current;
    if (!space) return;
    const onWheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;
      e.preventDefault();
      const now = Date.now();
      if (now - wheelLockRef.current < 260) return;
      wheelLockRef.current = now;
      setActive((a) =>
        Math.max(0, Math.min(spaceList.length - 1, a + (e.deltaX > 0 ? 1 : -1))),
      );
    };
    space.addEventListener("wheel", onWheel, { passive: false });
    return () => space.removeEventListener("wheel", onWheel);
  }, [mode, spaceList.length]);

  useEffect(() => {
    if (mode !== "space") return;
    const space = spaceRef.current;
    if (!space) return;
    let dragX: number | null = null;
    const onDown = (e: PointerEvent) => {
      if ((e.target as HTMLElement).closest(".snav")) return;
      dragX = e.clientX;
      movedRef.current = false;
    };
    const onUp = (e: PointerEvent) => {
      if (dragX === null) return;
      const dx = e.clientX - dragX;
      dragX = null;
      if (Math.abs(dx) > 50) {
        movedRef.current = true;
        setActive((a) =>
          Math.max(0, Math.min(spaceList.length - 1, a + (dx < 0 ? 1 : -1))),
        );
        window.setTimeout(() => {
          movedRef.current = false;
        }, 60);
      }
    };
    space.addEventListener("pointerdown", onDown);
    window.addEventListener("pointerup", onUp);
    return () => {
      space.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointerup", onUp);
    };
  }, [mode, spaceList.length]);

  const brandKeys = useMemo(
    () => (metrics ? Object.keys(metrics.by_brand).filter(Boolean).sort() : []),
    [metrics],
  );

  const bm = brand && metrics ? metrics.by_brand[brand] : undefined;
  const view = {
    total: bm ? bm.total_skus : metrics?.total_skus ?? 0,
    matched: bm ? bm.matched_skus : metrics?.matched_skus ?? 0,
    coverage: bm ? bm.coverage_pct : metrics?.coverage_pct ?? 0,
    opps: bm ? bm.back_in_stock_opportunities : metrics?.back_in_stock_opportunities ?? 0,
  };

  const v1 = useCountUp(view.total);
  const v2 = useCountUp(view.matched);
  const v3 = useCountUp(view.coverage, 2);
  const v4 = useCountUp(view.opps);

  const brandStats = useMemo(
    () =>
      metrics
        ? toSplit(
            Object.entries(metrics.by_brand) as [string, Record<string, number>][],
            prettyBrand,
            "brand",
          )
        : [],
    [metrics],
  );
  const shopStats = useMemo(
    () =>
      metrics
        ? toSplit(
            Object.entries(metrics.by_shop) as [string, Record<string, number>][],
            prettyShop,
            "shop",
          )
        : [],
    [metrics],
  );

  const goToCard = (i: number) => {
    if (!movedRef.current && i !== activeEff) setActive(i);
  };

  return (
    <div className="skuu-app">
      <div className="wrap">
        <header>
          <svg
            className="net"
            viewBox="0 0 400 140"
            preserveAspectRatio="xMaxYMid slice"
            aria-hidden="true"
          >
            <g stroke="#16a34a" strokeOpacity="0.25" strokeWidth="1.2" fill="none">
              <path d="M40 90 L120 40 L200 80 L280 30 L360 70" />
              <path d="M120 40 L150 120 L200 80" />
              <path d="M280 30 L300 110 L360 70" />
              <path d="M150 120 L300 110" strokeDasharray="4 5" />
            </g>
            <g fill="#16a34a">
              <circle className="p" cx="40" cy="90" r="4" />
              <circle className="p" cx="120" cy="40" r="5" style={{ animationDelay: ".4s" }} />
              <circle className="p" cx="200" cy="80" r="4" style={{ animationDelay: ".8s" }} />
              <circle className="p" cx="280" cy="30" r="5" style={{ animationDelay: "1.2s" }} />
              <circle className="p" cx="360" cy="70" r="4" style={{ animationDelay: "1.6s" }} />
              <circle className="p" cx="150" cy="120" r="3.5" style={{ animationDelay: "2s" }} />
              <circle className="p" cx="300" cy="110" r="3.5" style={{ animationDelay: "2.3s" }} />
            </g>
          </svg>
          <div>
            <div className="bars">
              <i style={{ background: "#16a34a" }} />
              <i style={{ background: "#84cc16" }} />
              <i style={{ background: "#f59e0b" }} />
              <i style={{ background: "#ef4444" }} />
              <i style={{ background: "#3b82f6" }} />
            </div>
            <div className="eyebrow">
              Engineering exercise · {brandKeys.length || 3} brands ·{" "}
              {metrics ? retailerCount(metrics.shops) : 9} retailers
            </div>
            <h1>
              What can go <b>back in stock</b>
            </h1>
          </div>
          <div className="head-actions">
            <button
              className="btn-ghost"
              onClick={refresh}
              disabled={loading}
              title="Reload metrics without re-scraping"
            >
              {loading ? "Refreshing…" : "Reload data"}
            </button>
            <button
              className="btn-primary"
              onClick={() => setRefreshOpen(true)}
              title="Re-scrape every shop, then recompute matches"
            >
              ↻ Refresh inventory
            </button>
          </div>
        </header>

        {!metrics && error && (
          <div className="err">
            <p>
              <b>Could not load the dashboard.</b> {error}
            </p>
            <p style={{ marginTop: 8, fontSize: 13 }}>
              The API is <code>{API_BASE}</code>. If this was a timeout, the
              first computation after a cold start can take a while; retrying
              usually reuses the backend&apos;s cached result.
            </p>
            <p style={{ marginTop: 12 }}>
              <button className="btn-ghost" onClick={refresh} disabled={loading}>
                {loading ? "Retrying…" : "Try again"}
              </button>
            </p>
          </div>
        )}

        {!metrics && loading && (
          <div className="loader">
            <div className="spinner" />
            <p>Computing cross-shop matches…</p>
          </div>
        )}

        {metrics && (
          <>
            <div className={`banner${error ? "" : " live"}`}>
              {error ? (
                <span>
                  <b>Refresh failed.</b> {error}. Showing the last loaded data.
                </span>
              ) : (
                <span>
                  <b>Live data.</b> {fmt(metrics.total_skus)} SKU rows ·{" "}
                  {metrics.shops.length} shops · engine v{metrics.algorithm_version}
                  {updatedAt ? ` · ${new Date(updatedAt).toLocaleString()}` : ""}
                </span>
              )}
            </div>

            <div className="tabs">
              <button
                className={`tab${brand === "" ? " on" : ""}`}
                onClick={() => changeBrand("")}
              >
                All brands<em>{fmt(metrics.total_skus)}</em>
              </button>
              {brandKeys.map((b) => (
                <button
                  key={b}
                  className={`tab${brand === b ? " on" : ""}`}
                  onClick={() => changeBrand(b)}
                >
                  {prettyBrand(b)}
                  <em>{fmt(metrics.by_brand[b].total_skus)}</em>
                </button>
              ))}
            </div>

            <div className="kpis">
              <div className="kpi k-a">
                <div className="k-head">
                  <span className="k-no">01</span>
                  <span className="k-ic">▦</span>
                </div>
                <h3>Total SKUs scraped</h3>
                <div className="v">{v1}</div>
                <DotMatrix coverage={view.coverage} />
                <p>One product, one size, one shop.</p>
              </div>
              <div className="kpi k-b">
                <div className="k-head">
                  <span className="k-no">02</span>
                  <span className="k-ic">⧉</span>
                </div>
                <h3>Matched SKUs</h3>
                <div className="v">{v2}</div>
                <div className="venn" aria-hidden="true">
                  <i />
                  <i />
                  <b />
                </div>
                <p>
                  {bm
                    ? "Same product found at other shops."
                    : `${fmt(metrics.matched_groups)} cross-shop groups.`}
                </p>
              </div>
              <div className="kpi k-c">
                <div className="k-head">
                  <span className="k-no">03</span>
                  <span className="k-ic">◔</span>
                </div>
                <h3>Coverage</h3>
                <div className="v">{v3}%</div>
                <Gauge coverage={view.coverage} />
                <p>Matched ÷ total scraped.</p>
              </div>
              <div className="kpi k-d">
                <div className="k-head">
                  <span className="k-no">04</span>
                  <span className="k-ic">⟲</span>
                </div>
                <h3>Back in stock</h3>
                <div className="v">{v4}</div>
                <div className="mini-route" aria-hidden="true">
                  <i className="o" />
                  <u />
                  <i className="g" />
                </div>
                <p>
                  {bm
                    ? "Sold out here, still available elsewhere."
                    : `${fmt(metrics.back_in_stock_skus)} sold-out SKUs recoverable.`}
                </p>
              </div>
            </div>

            <section>
              <h2>
                <span>Split</span>Per brand and per shop
              </h2>
              <div className="grid2">
                <div className="panel">
                  <div className="split-h">By brand</div>
                  <div>
                    {brandStats.map((s, i) => (
                      <SplitRow
                        key={s.name}
                        s={s}
                        best={i === 0 && brandStats.length > 1}
                      />
                    ))}
                  </div>
                </div>
                <div className="panel">
                  <div className="split-h">By shop</div>
                  <div>
                    {shopStats.map((s, i) => (
                      <SplitRow
                        key={s.name}
                        s={s}
                        best={i === 0 && shopStats.length > 1}
                      />
                    ))}
                  </div>
                </div>
              </div>
            </section>

            <section>
              <h2>
                <span>Detail</span>Back-in-stock opportunities
              </h2>
              <div className="panel">
                <div className="tools">
                  <input
                    type="search"
                    placeholder="Search style, colour…"
                    value={q}
                    onChange={(e) => changeQ(e.target.value)}
                    aria-label="Search opportunities"
                  />
                  <select
                    value={fShop}
                    onChange={(e) => changeShop(e.target.value)}
                    aria-label="Filter by sold-out shop"
                  >
                    <option value="">All sold-out shops</option>
                    {shopOptions.map((s) => (
                      <option key={s} value={s}>
                        {prettyShop(s)}
                      </option>
                    ))}
                  </select>
                  <div className="seg">
                    <button
                      className={mode === "space" ? "on" : ""}
                      onClick={() => setMode("space")}
                    >
                      ◎ Space
                    </button>
                    <button
                      className={mode === "grid" ? "on" : ""}
                      onClick={() => setMode("grid")}
                    >
                      ▦ Grid
                    </button>
                  </div>
                </div>

                {groupsError && (
                  <p className="rp-error">
                    Cross-shop routes could not be loaded: {groupsError}{" "}
                    <button
                      className="btn-ghost"
                      onClick={refresh}
                      disabled={loading}
                    >
                      Retry
                    </button>
                  </p>
                )}

                <p className="hint" style={{ margin: "0 0 12px" }}>
                  {fmt(list.length)} route{list.length === 1 ? "" : "s"} — sold out
                  in one shop while still available in another.
                </p>

                {mode === "space" ? (
                  <div className="space" ref={spaceRef}>
                    <div className="floor" />
                    <div className="ring" ref={ringRef}>
                      {spaceList.length === 0 ? (
                        <div
                          className="empty"
                          style={{
                            position: "absolute",
                            inset: 0,
                            display: "grid",
                            placeItems: "center",
                          }}
                        >
                          No routes found for this filter.
                        </div>
                      ) : (
                        spaceList.map((o, i) => (
                          <RouteCard
                            key={o.id}
                            o={o}
                            tilt={false}
                            active={i === activeEff}
                            onClick={() => goToCard(i)}
                          />
                        ))
                      )}
                    </div>
                    {spaceList.length > 0 && (
                      <>
                        <button
                          className="snav l"
                          onClick={() => setActive((a) => Math.max(0, a - 1))}
                          aria-label="Previous"
                        >
                          ‹
                        </button>
                        <button
                          className="snav r"
                          onClick={() =>
                            setActive((a) => Math.min(spaceList.length - 1, a + 1))
                          }
                          aria-label="Next"
                        >
                          ›
                        </button>
                        <div className="scount">
                          {activeEff + 1} / {spaceList.length}
                          {list.length > spaceList.length && (
                            <small>
                              top {spaceList.length} of {fmt(list.length)}
                            </small>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                ) : (
                  <>
                    <div className="cards">
                      {gridList.length === 0 ? (
                        <div className="empty">
                          No routes found for this filter.
                        </div>
                      ) : (
                        gridList.map((o, i) => (
                          <RouteCard
                            key={o.id}
                            o={o}
                            tilt
                            delay={Math.min(i, 12) * 30}
                          />
                        ))
                      )}
                    </div>
                    {list.length > shown && (
                      <button
                        className="more"
                        onClick={() => setShown((s) => s + 24)}
                      >
                        Show more routes ({list.length - shown} left)
                      </button>
                    )}
                  </>
                )}
              </div>
            </section>

            <footer>
              Live from the matching API at <code>{API_BASE}</code>. Match key:
              brand and size (both required) across different shops, plus equal
              normalised title + colour (or equal titles when both shops publish a
              compatible colour), and equal manufacturer style codes when both
              shops expose one. Salomon footwear sold in UK sizes is converted to
              the EU system so the brand&apos;s own store can be compared with EU
              retailers. Rows that cannot be matched are counted as scraped but not
              as matched. Opportunity routes list every matched back-in-stock group
              (mixed stock across shops).
            </footer>
          </>
        )}
      </div>
      <RefreshPanel
        open={refreshOpen}
        onClose={() => setRefreshOpen(false)}
        onFinished={refresh}
        shopLabel={prettyShop}
      />
    </div>
  );
}
