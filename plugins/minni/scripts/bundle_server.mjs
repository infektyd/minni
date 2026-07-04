#!/usr/bin/env node
/** Bundle dist/server.js into a single dependency-free ESM file (§4.2 step 2). */
import * as esbuild from "esbuild";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pluginRoot = join(here, "..");
const entry = join(pluginRoot, "dist", "server.js");
const outfile = entry;

await esbuild.build({
  entryPoints: [entry],
  bundle: true,
  platform: "node",
  format: "esm",
  outfile,
  allowOverwrite: true,
  external: ["node:*"],
  logLevel: "info",
});