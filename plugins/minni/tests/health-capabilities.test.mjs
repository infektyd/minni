import assert from "node:assert/strict";
import test from "node:test";

for (const enabled of [false, true]) {
  test(`health advertises deep research only when routes enabled (${enabled})`, async () => {
    const previous = process.env.MINNI_CONSOLE_DEEP_RESEARCH;
    process.env.MINNI_CONSOLE_DEEP_RESEARCH = enabled ? "1" : "0";
    let app;
    try {
      const { createUiServer } = await import(`../dist/ui-server.js?health-test=${enabled}`);
      app = createUiServer({host: "127.0.0.1", port: 0,
        deepResearch: {paths: async () => ({root: "fixture"})}});
      await app.start();
      const base = `http://127.0.0.1:${app.server.address().port}`;
      const health = await fetch(`${base}/api/health`).then((response) => response.json());
      assert.equal(health.tools.some((tool) => tool.startsWith("deep_research_")), enabled);
      assert.ok(health.tools.includes("minni_prepare_task"));
      const route = await fetch(`${base}/api/deep-research/paths`, {headers: {Authorization: `Bearer ${process.env.MINNI_CONSOLE_TOKEN}`}});
      assert.equal(route.status, enabled ? 200 : 403);
    } finally {
      if (app) await app.close();
      if (previous === undefined) delete process.env.MINNI_CONSOLE_DEEP_RESEARCH;
      else process.env.MINNI_CONSOLE_DEEP_RESEARCH = previous;
    }
  });
}
