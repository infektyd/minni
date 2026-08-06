// Determinism of built_at derivation (#352).
import assert from "node:assert/strict";
import test from "node:test";

import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { deterministicBuiltAt } from "../scripts/emit_build_manifest.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

test("deterministicBuiltAt equals the actual git HEAD commit date", () => {
  const prev = process.env.SOURCE_DATE_EPOCH;
  delete process.env.SOURCE_DATE_EPOCH;
  try {
    const a = deterministicBuiltAt();
    const b = deterministicBuiltAt();
    assert.equal(a, b);
    // Must look like 2026-08-06T02:26:47Z (UTC Z, no millis)
    assert.match(a, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    // Pin to the real HEAD date: two wall-clock calls in the same millisecond
    // would also satisfy a === b, so equality alone cannot catch a regression
    // to new Date().
    const raw = execFileSync("git", ["log", "-1", "--format=%cI"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
    }).trim();
    const expected = new Date(raw).toISOString().replace(/\.\d{3}Z$/, "Z");
    assert.equal(a, expected);
  } finally {
    if (prev === undefined) delete process.env.SOURCE_DATE_EPOCH;
    else process.env.SOURCE_DATE_EPOCH = prev;
  }
});

test("deterministicBuiltAt honors SOURCE_DATE_EPOCH when set", () => {
  const prev = process.env.SOURCE_DATE_EPOCH;
  try {
    process.env.SOURCE_DATE_EPOCH = "1609459200"; // 2021-01-01T00:00:00Z
    const a = deterministicBuiltAt();
    assert.equal(a, "2021-01-01T00:00:00Z");
    const b = deterministicBuiltAt();
    assert.equal(b, "2021-01-01T00:00:00Z");
  } finally {
    if (prev === undefined) delete process.env.SOURCE_DATE_EPOCH;
    else process.env.SOURCE_DATE_EPOCH = prev;
  }
});

test("deterministicBuiltAt with SOURCE_DATE_EPOCH=0 yields epoch", () => {
  const prev = process.env.SOURCE_DATE_EPOCH;
  try {
    process.env.SOURCE_DATE_EPOCH = "0";
    assert.equal(deterministicBuiltAt(), "1970-01-01T00:00:00Z");
  } finally {
    if (prev === undefined) delete process.env.SOURCE_DATE_EPOCH;
    else process.env.SOURCE_DATE_EPOCH = prev;
  }
});

test("strict parse rejects what both sides must reject (parity table)", () => {
  const prev = process.env.SOURCE_DATE_EPOCH;
  try {
    delete process.env.SOURCE_DATE_EPOCH;
    const expected = deterministicBuiltAt();
    for (const bad of ["1.5", "0x10", "1_000", "1e30", "Infinity",
                       "99999999999999999999", "1754447207000000", " ", "abc"]) {
      process.env.SOURCE_DATE_EPOCH = bad;
      assert.equal(deterministicBuiltAt(), expected, `epoch ${bad} must fall through`);
    }
    // Boundary accepted on both sides (year 9999) — mirrors the Python pin.
    process.env.SOURCE_DATE_EPOCH = "253402300799";
    assert.equal(deterministicBuiltAt(), "9999-12-31T23:59:59Z");
  } finally {
    if (prev === undefined) delete process.env.SOURCE_DATE_EPOCH;
    else process.env.SOURCE_DATE_EPOCH = prev;
  }
});
