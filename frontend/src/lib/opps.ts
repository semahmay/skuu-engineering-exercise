/* Pure, React-free helpers for turning matched groups into dashboard routes.
 *
 * Kept in a standalone module (no JSX / "use client") so the id logic can be
 * unit-tested with Node's built-in test runner.
 */

export type GroupMember = {
  shop: string;
  title?: string | null;
  colour?: string | null;
  derived_colour?: string | null;
  size?: string | null;
  price?: number | string | null;
  currency?: string | null;
  in_stock: boolean;
  url?: string | null;
};

export type Group = {
  brand: string;
  size: string;
  shops: string[];
  has_mixed_stock: boolean;
  members: GroupMember[];
};

export type Opp = {
  id: string;
  brand: string;
  title: string;
  colour: string;
  size: string;
  price: number | string | null;
  currency: string;
  shop: string;
  alts: string[];
};

/**
 * Deterministic, unique id for a back-in-stock opportunity.
 *
 * The identity is the *sold-out SKU itself*, not its position in an array: a
 * shop's product URL is unique per SKU (the collection enforces a unique
 * shop+url+size index), so two colourways of the same product+size in the same
 * shop get different ids. Title/colour are used only as a fallback when the URL
 * is missing, which keeps the id stable across refetches of identical data.
 */
export function oppId(brand: string, size: string, m: GroupMember): string {
  const url = m.url ? String(m.url).trim() : "";
  const identity = url
    ? url
    : `${m.title ?? ""}~${m.colour ?? ""}~${m.derived_colour ?? ""}`;
  return [brand, size, m.shop, identity].join("|");
}

export function buildOpps(groups: Group[]): Opp[] {
  const out: Opp[] = [];
  groups.forEach((g) => {
    if (!g.has_mixed_stock) return;
    const inShops = [
      ...new Set(g.members.filter((m) => m.in_stock).map((m) => m.shop)),
    ];
    if (!inShops.length) return;
    const base = g.members[0];
    g.members
      .filter((m) => !m.in_stock)
      .forEach((m) => {
        const alts = inShops.filter((s) => s !== m.shop);
        if (!alts.length) return;
        out.push({
          id: oppId(g.brand, g.size, m),
          brand: g.brand,
          title: String(m.title ?? base?.title ?? g.brand),
          colour: String(m.colour ?? m.derived_colour ?? base?.colour ?? ""),
          size: String(m.size ?? g.size ?? ""),
          price: m.price ?? base?.price ?? null,
          currency: String(m.currency ?? base?.currency ?? ""),
          shop: m.shop,
          alts,
        });
      });
  });
  return out;
}
