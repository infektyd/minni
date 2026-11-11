#!/usr/bin/env node
/** Bundle dist/server.js into a single dependency-free ESM file (§4.2 step 2).
 *
 * Accepts `--outfile <path>` or `--outfile=<path>` (resolved against
 * `process.cwd()`) so payload staging never overwrites `tsc`'s modular
 * `dist/server.js` in place (BUG-06). Defaults to `entry` only when `--outfile`
 * is omitted.
 */
import * as esbuild from "esbuild";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pluginRoot = resolve(join(here, ".."));
const entry = join(pluginRoot, "dist", "server.js");

function parseOutfileArg(argv) {
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--outfile") {
      const next = argv[i + 1];
      if (!next || next.startsWith("--")) {
        throw new Error("bundle_server.mjs: --outfile requires a path argument");
      }
      return resolve(process.cwd(), next);
    }
    if (arg.startsWith("--outfile=")) {
      const val = arg.slice("--outfile=".length);
      if (!val) {
        throw new Error("bundle_server.mjs: --outfile= requires a non-empty path");
      }
      return resolve(process.cwd(), val);
    }
  }
  return entry;
}

const outfile = parseOutfileArg(process.argv.slice(2));

await esbuild.build({
  absWorkingDir: pluginRoot,
  entryPoints: [entry],
  bundle: true,
  platform: "node",
  format: "esm",
  outfile,
  allowOverwrite: true,
  external: ["node:*"],
  logLevel: "info",
});
