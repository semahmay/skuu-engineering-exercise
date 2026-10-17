import { test } from "node:test";
import assert from "node:assert/strict";

import { buildOpps, oppId } from "../src/lib/opps.ts";
import type { Group, GroupMember } from "../src/lib/opps.ts";

const member = (over: Partial<GroupMember>): GroupMember => ({
  shop: "caliroots",
  title: "OG Detroit Jacket",
  colour: "",
  derived_colour: "",
  size: "L",
  price: 100,
  currency: "EUR",
  in_stock: false,
  url: "https://caliroots.com/products/og-detroit-jacket",
  ...over,
});

/**
 * Two distinct colourways of the same brand+size, each sold out in the SAME
 * shop (caliroots) and in stock elsewhere. Under the old id scheme
 * (`brand|size|shop|index`) both reduce to `carhartt wip|l|caliroots|0`.
 */
const collisionGroups: Group[] = [
  {
    brand: "carhartt wip",
    size: "l",
    shops: ["caliroots", "xtreme"],
    has_mixed_stock: true,
    members: [
      member({ url: "https://caliroots.com/products/detroit-black" }),
      member({
        shop: "xtreme",
        in_stock: true,
        url: "https://xtreme.pt/products/detroit-black",
      }),
    ],
  },
  {
    brand: "carhartt wip",
    size: "l",
    shops: ["caliroots", "xtreme"],
    has_mixed_stock: true,
    members: [
      member({ url: "https://caliroots.com/products/detroit-brown" }),
      member({
        shop: "xtreme",
        in_stock: true,
        url: "https://xtreme.pt/products/detroit-brown",
      }),
    ],
  },
];

const assertAllUnique = (ids: string[]) => {
  const dup = [...new Set(ids.filter((id, i) => ids.indexOf(id) !== i))];
  assert.equal(ids.length, new Set(ids).size, `duplicate ids: ${dup.join(", ")}`);
};

test("oppId distinguishes two colourways of the same product+size+shop (regression)", () => {
  const a = oppId("carhartt wip", "l", collisionGroups[0].members[0]);
  const b = oppId("carhartt wip", "l", collisionGroups[1].members[0]);
  assert.notEqual(a, b);
});

test("buildOpps yields unique ids for the colourway-collision case", () => {
  assertAllUnique(buildOpps(collisionGroups).map((o) => o.id));
});

test("oppId is deterministic and stable across calls", () => {
  const m = collisionGroups[0].members[0];
  assert.equal(oppId("carhartt wip", "l", m), oppId("carhartt wip", "l", m));
});

test("oppId falls back to title/colour when the URL is missing", () => {
  const a = oppId("veja", "40", member({ url: null, title: "Campo", colour: "White" }));
  const b = oppId("veja", "40", member({ url: null, title: "Campo", colour: "Black" }));
  assert.notEqual(a, b);
});

test("buildOpps produces globally unique ids for a realistic mixed set", () => {
  const groups: Group[] = [];
  for (const size of ["s", "m", "l", "xl"]) {
    for (let c = 0; c < 6; c++) {
      groups.push({
        brand: "carhartt wip",
        size,
        shops: ["caliroots", "xtreme", "footway"],
        has_mixed_stock: true,
        members: [
          member({ size, url: `https://caliroots.com/products/x-${size}-${c}` }),
          member({
            shop: "xtreme",
            size,
            in_stock: true,
            url: `https://xtreme.pt/products/x-${size}-${c}`,
          }),
          member({
            shop: "footway",
            size,
            in_stock: true,
            url: `https://footway.com/products/x-${size}-${c}`,
          }),
        ],
      });
    }
  }
  assertAllUnique(buildOpps(groups).map((o) => o.id));
});
