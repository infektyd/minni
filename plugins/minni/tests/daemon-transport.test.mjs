import assert from "node:assert/strict";
import test from "node:test";

import { extractDaemonLead, formatRecall } from "../dist/daemon_transport.js";

test("The Bracket Bug is dead: formatRecall does not output 'Daemon lead: ['", () => {
  const response = {
    backend: "faiss-disk",
    agent_id: "codex",
    results: [
      {
        wikilink: "[[wiki/sessions/socket-health]]",
        heading: "Socket Health Check",
        score: 0.95,
        snippet: "Verify ~/.minni/run/minnid.sock before calling recall.",
      },
    ],
  };

  const formatted = formatRecall("socket health", response, []);
  assert.ok(!formatted.includes("Daemon lead: ["), "must never render stringified JSON bracket");
  assert.doesNotMatch(formatted, /Daemon lead: \[/);
});

test("extractDaemonLead safely extracts heading or title and never returns a bracket", () => {
  const arrayWithHeading = [
    {
      wikilink: "[[wiki/sessions/socket-health]]",
      heading: "Socket Health Check",
      score: 0.95,
    },
  ];
  assert.equal(extractDaemonLead(arrayWithHeading), "Socket Health Check");

  const arrayWithTitle = [
    {
      wikilink: "[[wiki/sessions/config]]",
      title: "Configuration Reference",
      score: 0.88,
    },
  ];
  assert.equal(extractDaemonLead(arrayWithTitle), "Configuration Reference");

  const arrayEmpty = [];
  assert.equal(extractDaemonLead(arrayEmpty), undefined);

  const rawJsonArrayString = "[\n  {\n    \"wikilink\": \"[[wiki/a]]\"\n  }\n]";
  assert.equal(extractDaemonLead(rawJsonArrayString), undefined);

  const plainString = "### socket-health.md (score=1.000)\nUse minnid.sock";
  assert.equal(extractDaemonLead(plainString), "### socket-health.md (score=1.000)");
});

test("AI Context Pack renders clean Markdown cards for authoritative daemon hits", () => {
  const response = {
    backend: "faiss-disk",
    agent_id: "codex",
    results: [
      {
        wikilink: "[[wiki/sessions/socket-health]]",
        heading: "Socket Health Check",
        score: 0.954,
        snippet: "  Verify   ~/.minni/run/minnid.sock   before calling recall.  ",
      },
      {
        filename: "wiki/guides/setup.md",
        title: "Setup Guide",
        score: 0.82,
        content: "Run make setup to bootstrap the environment.",
      },
    ],
  };

  const formatted = formatRecall("socket health", response, []);

  assert.match(formatted, /## AI Context Pack/);
  // Card 1
  assert.match(
    formatted,
    /1\. \[\[wiki\/sessions\/socket-health\]\] — Socket Health Check \(score=0\.95\)\n {3}Verify ~\/\.minni\/run\/minnid\.sock before calling recall\./,
  );
  // Card 2: filename normalized to wikilink, title displayed, content used as snippet
  assert.match(
    formatted,
    /2\. \[\[wiki\/guides\/setup\.md\]\] — Setup Guide \(score=0\.82\)\n {3}Run make setup to bootstrap the environment\./,
  );
});

test("Ghost Miss Header is suppressed when authoritative daemon hits exist", () => {
  const response = {
    backend: "faiss-disk",
    agent_id: "codex",
    results: [
      {
        wikilink: "[[wiki/sessions/socket-health]]",
        score: 0.95,
        snippet: "Active daemon response.",
      },
    ],
  };

  const formatted = formatRecall("socket health", response, []);
  assert.doesNotMatch(formatted, /No .* vault wiki matches\./);
  assert.ok(!formatted.includes("No Codex vault wiki matches."));
});

test("Ghost Miss Header is suppressed when local vault results exist", () => {
  const response = {
    results: [],
    count: 0,
    agent_id: "codex",
  };
  const vaultResults = [
    {
      notePath: "/tmp/vault/wiki/sessions/socket-health.md",
      relativePath: "wiki/sessions/socket-health.md",
      wikilink: "[[wiki/sessions/socket-health]]",
      title: "Socket Health",
      snippet: "Local vault match found.",
      score: 61,
    },
  ];

  const formatted = formatRecall("socket health", response, vaultResults);
  assert.doesNotMatch(formatted, /No .* vault wiki matches\./);
  assert.match(formatted, /1\. \[\[wiki\/sessions\/socket-health\]\] — Socket Health \(vault score=61\)/);
});

test("Ghost Miss Header appears only when BOTH daemon and vault return zero hits", () => {
  const responseCodex = {
    results: [],
    count: 0,
    agent_id: "codex",
  };
  const formattedCodex = formatRecall("empty query", responseCodex, []);
  assert.match(formattedCodex, /No Codex vault wiki matches\./);

  const responseGemini = {
    results: [],
    count: 0,
    agent_id: "gemini",
  };
  const formattedGemini = formatRecall("empty query", responseGemini, []);
  assert.match(formattedGemini, /No Gemini vault wiki matches\./);

  const responseClaude = {
    results: [],
    count: 0,
    agent_id: "claude-code",
  };
  const formattedClaude = formatRecall("empty query", responseClaude, []);
  assert.match(formattedClaude, /No Claude Code vault wiki matches\./);
});

test("Raw JSON dump under ## Daemon Results is omitted by default when AI Context Pack cards render", () => {
  const response = {
    backend: "faiss-disk",
    agent_id: "codex",
    results: [
      {
        wikilink: "[[wiki/sessions/socket-health]]",
        heading: "Socket Health Check",
        score: 0.95,
        snippet: "Active daemon response.",
      },
    ],
  };

  const formatted = formatRecall("socket health", response, []);
  assert.doesNotMatch(formatted, /## Daemon Results/);
  assert.ok(!formatted.includes('"wikilink": "[[wiki/sessions/socket-health]]"'));
});

test("Raw JSON dump under ## Daemon Results is preserved when debug mode is requested", () => {
  const response = {
    backend: "faiss-disk",
    agent_id: "codex",
    results: [
      {
        wikilink: "[[wiki/sessions/socket-health]]",
        heading: "Socket Health Check",
        score: 0.95,
        snippet: "Active daemon response.",
      },
    ],
  };

  // Explicit options debug flag
  const formattedDebugOption = formatRecall("socket health", response, [], { debug: true });
  assert.match(formattedDebugOption, /## Daemon Results/);
  assert.ok(formattedDebugOption.includes('"wikilink": "[[wiki/sessions/socket-health]]"'));

  // Boolean debug parameter
  const formattedDebugBool = formatRecall("socket health", response, [], true);
  assert.match(formattedDebugBool, /## Daemon Results/);

  // Environment variable debug mode
  process.env.MINNI_DEBUG_RECALL = "true";
  try {
    const formattedDebugEnv = formatRecall("socket health", response, []);
    assert.match(formattedDebugEnv, /## Daemon Results/);
  } finally {
    delete process.env.MINNI_DEBUG_RECALL;
  }
});

test("Continuous card numbering when both daemon hits and vault hits are passed", () => {
  const response = {
    backend: "faiss-disk",
    agent_id: "codex",
    results: [
      {
        wikilink: "[[wiki/sessions/daemon-hit]]",
        heading: "Daemon Hit",
        score: 0.99,
        snippet: "From daemon.",
      },
    ],
  };
  const vaultResults = [
    {
      notePath: "/tmp/vault/wiki/sessions/vault-hit.md",
      relativePath: "wiki/sessions/vault-hit.md",
      wikilink: "[[wiki/sessions/vault-hit]]",
      title: "Vault Hit",
      snippet: "From vault.",
      score: 50,
    },
  ];

  const formatted = formatRecall("test", response, vaultResults);
  assert.match(formatted, /1\. \[\[wiki\/sessions\/daemon-hit\]\] — Daemon Hit/);
  assert.match(formatted, /2\. \[\[wiki\/sessions\/vault-hit\]\] — Vault Hit/);
});
