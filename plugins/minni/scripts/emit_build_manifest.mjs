#!/usr/bin/env node
/**
 * Stamp a build manifest into plugins/minni/dist.
 *
 * The plugin build is copied into every agent runtime that uses it -- Claude
 * Code, Codex, KiloCode, the shared ~/.agents tree, and the wheel payload. Once
 * copied, a dist directory carries no record of what it was built from, so a
 * runtime can run a build that is weeks behind source with nothing to reveal it.
 * Observed: four runtimes sitting two days behind while the fix they needed was
 * already committed, and a wheel payload three weeks behind.
 *
 * Mirrors the schema scripts/stage_payload.py already writes for the wheel
 * payload, so one reader (scripts/check_deployments.py) can compare every
 * deployment against source HEAD.
 */
import { execFileSync } from "node:child_process";
import { writeFileSync, readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = path.resolve(PLUGIN_ROOT, "..", "..");
const DIST = path.join(PLUGIN_ROOT, "dist");

function gitSha() {
  try {
    return execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
    }).trim() || "unknown";
  } catch {
    return "unknown";
  }
}

function gitDirty() {
  try {
    const out = execFileSync("git", ["status", "--porcelain"], {
      cwd: REPO_ROOT,
      encoding: "utf8",
    });
    // A dirty tree means the sha alone does not identify what was built.
    return out.trim().length > 0;
  } catch {
    return false;
  }
}

function version() {
  try {
    const pkg = JSON.parse(readFileSync(path.join(PLUGIN_ROOT, "package.json"), "utf8"));
    return pkg.version ?? "unknown";
  } catch {
    return "unknown";
  }
}

if (!existsSync(DIST)) {
  console.error(`emit_build_manifest: no dist at ${DIST} — run the build first`);
  process.exit(1);
}

const manifest = {
  schema: 1,
  version: version(),
  git_sha: gitSha(),
  git_dirty: gitDirty(),
  built_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
  built_from: REPO_ROOT,
};

const out = path.join(DIST, "build-manifest.json");
writeFileSync(out, JSON.stringify(manifest, null, 2) + "\n", "utf8");
console.log(
  `build manifest: ${manifest.version} ${manifest.git_sha.slice(0, 8)}` +
    `${manifest.git_dirty ? "-dirty" : ""} -> ${path.relative(REPO_ROOT, out)}`,
);
