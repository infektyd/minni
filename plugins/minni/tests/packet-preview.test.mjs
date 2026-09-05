import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import { renderToStaticMarkup } from "react-dom/server";

const bundle = await build({
  entryPoints: ["frontend-src/src/screens/PacketScreen.tsx"],
  bundle: true, write: false, platform: "node", format: "esm", jsx: "automatic",
});
const { PacketScreen } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

function descendants(element) {
  if (!element || typeof element !== "object") return [];
  const children = [element.props?.children, element.props?.actions];
  return [element, ...[children].flat(Infinity).flatMap(descendants)];
}

for (const markdown of ["# Prepared context\nSource Alpha and Source Beta", ""]) {
  test(`packet preview and copied context ignore inspection marks (${markdown ? "markdown" : "json"})`, async () => {
    const sources = ["Alpha", "Beta"].map((name) => ({title: name, relativePath: `${name}.md`, snippet: name, score: 1, privacyLevel: "safe"}));
    const packet = {task: "test", profile: "compact", mode: "deterministic", budgetTokens: 1500,
      budget: {tokens: 1500}, recall: {daemonOk: true}, afm: {used: false}, risks: [],
      relevantSources: sources, contextMarkdown: markdown};
    const selected = new Set();
    const tree = PacketScreen({packet, evidence: [], selected});
    const html = renderToStaticMarkup(tree);
    assert.match(html, /Prepared sources/);
    assert.match(html, /Alpha\.md/);
    assert.match(html, /Beta\.md/);
    assert.match(html, /inspection marks/);
    assert.match(html, /Token allowance/);
    assert.doesNotMatch(html, /class="meter"/);
    const copy = descendants(tree).find((element) => element.type === "button" && element.props.children === "Copy");
    const previous = Object.getOwnPropertyDescriptor(globalThis, "navigator");
    let copied;
    Object.defineProperty(globalThis, "navigator", {configurable: true, value: {clipboard: {writeText: async (value) => {copied = value;}}}});
    try { copy.props.onClick(); } finally {
      if (previous) Object.defineProperty(globalThis, "navigator", previous);
      else delete globalThis.navigator;
    }
    if (markdown) assert.equal(copied, markdown);
    else assert.deepEqual(JSON.parse(copied).relevantSources.map((source) => source.title), ["Alpha", "Beta"]);
  });
}
