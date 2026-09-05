// Exercise the actual frontend mappers and rendered privacy chip.
import { test } from "node:test";
import assert from "node:assert/strict";
import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const output = new URL("./.compiled/api-mapping-test.mjs", import.meta.url);
await build({
  stdin: {
    contents: 'export { classFromPath, privacyFromLevel, authorityFromSource, afmForSource, evidenceFromSource } from "./api"; export { PrivacyChip, AuthorityChip, AfmChip } from "./components/Chip";',
    resolveDir: fileURLToPath(new URL("../frontend-src/src", import.meta.url)),
    loader: "tsx",
  },
  outfile: fileURLToPath(output), bundle: true, platform: "node", format: "esm",
  packages: "external", jsx: "automatic", logLevel: "silent",
});
const { classFromPath, privacyFromLevel, authorityFromSource, afmForSource, evidenceFromSource, PrivacyChip, AuthorityChip, AfmChip } = await import(output.href);

test("classFromPath classifies vault sub-trees", () => {
  assert.equal(classFromPath("vault/wiki/handoffs/v4-envelope-schema.md"), "wiki");
  assert.equal(classFromPath("vault/raw/sessions/2026-04-21/compact.json"), "raw");
  assert.equal(classFromPath("vault/logs/hygiene/2026-04-23-prune.log"), "log");
  assert.equal(classFromPath("vault/inbox/2026-04-26T11-40_gemini.json"), "inbox");
  assert.equal(classFromPath(""), "other");
  assert.equal(classFromPath(undefined), "other");
});

test("privacyFromLevel maps daemon privacy onto the UI shape", () => {
  assert.equal(privacyFromLevel("private"), "private");
  assert.equal(privacyFromLevel("blocked"), "blocked");
  assert.equal(privacyFromLevel("local-only"), "local-only");
  assert.equal(privacyFromLevel("safe"), "safe");
  assert.equal(privacyFromLevel(undefined), "unknown");
});

test("source authority is provenance, not a sharing or ownership grant", () => {
  for (const authority of ["handoff", "decision", "schema", "session", "concept", "daemon", "vault", undefined, "unrecognized"]) {
    const expected = authority === undefined || authority === "unrecognized" ? "unknown" : authority;
    assert.equal(authorityFromSource(authority), expected);
    const row = evidenceFromSource({ title: "Source", relativePath: "wiki/source.md", snippet: "evidence", score: 1, authority });
    assert.equal(row.authority, expected);
    const html = renderToStaticMarkup(createElement(AuthorityChip, { value: row.authority }));
    assert.ok(html.includes(expected.toUpperCase()));
    assert.doesNotMatch(html, /OWNER|TEAM|PUBLIC|SYSTEM/);
  }
});

test("afmForSource picks dns when source is blocked or do-not-store", () => {
  const blocked = { relativePath: "x", title: "x", privacyLevel: "blocked" };
  assert.equal(afmForSource(blocked, undefined), "dns");

  const src = { relativePath: "vault/raw/foo.md", title: "Foo" };
  const out = {
    learnCandidates: [],
    logOnly: [],
    expires: [],
    doNotStore: ["Found vault/raw/foo.md in pruning"],
  };
  assert.equal(afmForSource(src, out), "dns");
});

test("afmForSource prefers dns over log over learn", () => {
  const src = { relativePath: "vault/raw/foo.md", title: "Foo" };
  const all = {
    learnCandidates: ["Foo"],
    logOnly: ["vault/raw/foo.md"],
    expires: [],
    doNotStore: ["vault/raw/foo.md"],
  };
  assert.equal(afmForSource(src, all), "dns");
  const log = { ...all, doNotStore: [] };
  assert.equal(afmForSource(src, log), "log");
  const learn = { ...log, logOnly: [] };
  assert.equal(afmForSource(src, learn), "learn");
});

test("afmForSource stays unclassified when no outcome match", () => {
  const src = { relativePath: "vault/wiki/foo.md", title: "Foo" };
  assert.equal(afmForSource(src, undefined), "unclassified");
  assert.equal(
    afmForSource(src, {
      learnCandidates: ["bar"],
      logOnly: ["baz"],
      expires: [],
      doNotStore: [],
    }),
    "unclassified",
  );
});

test("source privacy survives mapping and rendering without invented sharing grants", () => {
  for (const level of ["safe", "local-only", "private", "blocked", undefined, "unrecognized"]) {
    const expected = level === undefined || level === "unrecognized" ? "unknown" : level;
    const row = evidenceFromSource({
      title: "Source", relativePath: "wiki/source.md", snippet: "evidence", score: 1,
      privacyLevel: level,
    });
    assert.equal(row.privacy, expected);
    const html = renderToStaticMarkup(createElement(PrivacyChip, { value: row.privacy }));
    assert.ok(html.includes(expected.toUpperCase()));
    assert.doesNotMatch(html, /PUBLIC|TEAM/);
    if (level === "blocked") {
      assert.equal(row.private, true);
      assert.equal(row.selected, false);
      assert.equal(row.afm, "dns");
    }
  }
});

test("unassessed sources remain selected without an AFM safety assertion", () => {
  const row = evidenceFromSource({ title: "Source", relativePath: "wiki/source.md", snippet: "text", score: 1, privacyLevel: "safe" });
  assert.equal(row.afm, "unclassified");
  assert.equal(row.selected, true);
  const html = renderToStaticMarkup(createElement(AfmChip, { value: row.afm }));
  assert.match(html, /UNCLASSIFIED/);
  assert.doesNotMatch(html, /AFM-SAFE/);
});
