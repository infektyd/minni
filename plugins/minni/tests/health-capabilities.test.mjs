import assert from "node:assert/strict";
import test from "node:test";

test("health advertises core Minni tools and omits removed foreign deep_research tools", async () => {
  let app;
  try {
    const { createUiServer } = await import("../dist/ui-server.js");
    app = createUiServer({ host: "127.0.0.1", port: 0 });
    await app.start();
    const base = `http://127.0.0.1:${app.server.address().port}`;
    const health = await fetch(`${base}/api/health`).then((response) => response.json());
    assert.equal(health.tools.some((tool) => tool.startsWith("deep_research_")), false);
    assert.ok(health.tools.includes("minni_prepare_task"));
    const route = await fetch(`${base}/api/deep-research/paths`, {
      headers: { Authorization: `Bearer ${process.env.MINNI_CONSOLE_TOKEN}` },
    });
    assert.equal(route.status, 404);
  } finally {
    if (app) await app.close();
  }
});

