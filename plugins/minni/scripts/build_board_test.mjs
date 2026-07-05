#!/usr/bin/env node
/** Emit tests/.compiled/board-test.mjs — board pure-logic modules for node:test on Node 20. */
import * as esbuild from "esbuild";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pluginRoot = join(here, "..");
const outDir = join(pluginRoot, "tests", ".compiled");

mkdirSync(outDir, { recursive: true });

await esbuild.build({
  entryPoints: [join(pluginRoot, "frontend-src/src/board/boardTestExports.ts")],
  bundle: true,
  platform: "neutral",
  format: "esm",
  target: "es2022",
  outfile: join(outDir, "board-test.mjs"),
  logLevel: "warning",
});
