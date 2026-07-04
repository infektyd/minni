import assert from "node:assert/strict";
import test from "node:test";

import { assessLearningQuality, detectSecretMaterial } from "../dist/policy.js";

// #138: the gate must flag credential MATERIAL, not credential VOCABULARY.

const GOOD_INPUT = {
  title: "PyPI trusted publisher OIDC claim casing",
  category: "procedures",
  source: "session 2026-07-03",
};

function assess(content) {
  return assessLearningQuality({ ...GOOD_INPUT, content });
}

test("credential vocabulary does not block: id-token, tokenizer, api-key docs (#138)", () => {
  const notes = [
    // The exact class of note that was blocked three times while dogfooding v0.2.
    "PyPI trusted publishing requires the GitHub Actions permission id-token: write, and the publisher registration must match the lowercase OIDC repository claim.",
    "The tokenizer budget is 4096 tokens per context window; tiktoken counts them differently than the API token_count field reports.",
    "Never store the api key in the vault; the old token was revoked after the incident and rotation is documented in the runbook.",
    "Secret handling procedure: secrets belong in the keychain, never in memory notes; password rotation happens quarterly.",
  ];
  for (const content of notes) {
    assert.equal(detectSecretMaterial(content), null, content.slice(0, 60));
    const report = assess(content);
    assert.equal(report.ok, true, report.summary);
  }
});

test("well-known secret prefixes block", () => {
  // Fixture "secrets" are split-and-joined so GitHub push protection and
  // other file-level scanners don't flag the test file itself (#138 irony).
  const j = (...parts) => parts.join("");
  const secrets = [
    j("the publish token is pypi-", "AgEIcHlwaS5vcmcCJGNkYmYzdjA0LWQ5NGItNDdkYQ"),
    j("ghp_", "AbCdEf0123456789AbCdEf0123456789AbCd", " was pasted in the log"),
    j("github_pat_", "11ABCDEFG0abcdefghijklmnopqrstuvwxyz"),
    j("sk-", "proj-Ab12Cd34Ef56Gh78Ij90KlMnOpQrStUv"),
    j("xoxb-", "1234567890-abcdefghijklmn"),
    j("creds AKIA", "IOSFODNN7EXAMPLE", " in the env"),
    j("-----BEGIN RSA ", "PRIVATE KEY-----"),
    j("bearer eyJ", "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"),
  ];
  for (const content of secrets) {
    assert.notEqual(detectSecretMaterial(content), null, content.slice(0, 40));
    const report = assess(
      `${content} — plus enough surrounding words to clear the short-content warning for this test case.`,
    );
    assert.equal(report.ok, false);
    assert.ok(report.warnings.some((w) => w.includes("sensitive material")));
  }
});

test("credential keyword assigned an opaque literal blocks; bare mention does not", () => {
  assert.notEqual(detectSecretMaterial("api_key = h8f3kd92mfp1qz7w"), null);
  assert.notEqual(detectSecretMaterial('password: "correcthorsebatterystaple"'), null);
  // Codex P1 (PR #146): punctuation-bearing and quoted-with-spaces values.
  assert.notEqual(detectSecretMaterial('password: "aB3!dE5@gH7#jK9%"'), null);
  assert.notEqual(detectSecretMaterial('secret = "horse battery staple correct"'), null);
  assert.notEqual(detectSecretMaterial('"api_key": "h8f3kd92mfp1qz7w"'), null);
  // GitHub Actions permission syntax — keyword + colon but no opaque literal.
  assert.equal(detectSecretMaterial("permissions:\n  id-token: write"), null);
  assert.equal(detectSecretMaterial("the token was revoked yesterday"), null);
  // Unquoted prose word after a colon must not match (no digit).
  assert.equal(detectSecretMaterial("token: authentication-related notes"), null);
});

test("high-entropy opaque strings block; SHAs, digests, paths, URLs do not", () => {
  assert.notEqual(
    detectSecretMaterial("value zK9mQ2xVb7Rf4Wc8Ln3Jp6Ht1Dg5Ys0A found inline"),
    null,
  );
  const benign = [
    // git SHA + sha256 digest: hex has no uppercase, must not match.
    "fixed in commit f398473b2c334af66d9e88a1b0c7e989c7e989bd",
    "wheel sha256=284d14881fdf1a58a70bebfb0dd92f5140f2253acb10524fc259b43065c023d1",
    "path /Users/hansaxelsson/Projects/Minni/plugins/minni/dist/server.js",
    "see https://github.com/infektyd/minni/actions/runs/28714837391",
  ];
  for (const content of benign) {
    assert.equal(detectSecretMaterial(content), null, content.slice(0, 50));
  }
});

test("secret material is a hard gate regardless of an otherwise strong score", () => {
  const report = assessLearningQuality({
    title: "A perfectly titled durable procedure note",
    category: "procedures",
    source: "session",
    content:
      "This otherwise excellent and complete note accidentally embeds ghp_AbCdEf0123456789AbCdEf0123456789AbCd from a paste and must be blocked.",
  });
  assert.equal(report.ok, false);
});
