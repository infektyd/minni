import assert from "node:assert/strict";
import test from "node:test";

import {
  assessLearningQuality,
  assessLearningQualityAsync,
  detectSecretMaterial,
  findInconclusiveHighRiskAssignments,
} from "../dist/policy.js";
import {
  classifyInconclusiveWithAfmImpl,
  parseInconclusiveAfmVerdict,
} from "../dist/policy-secret-afm.js";

const GOOD_INPUT = {
  title: "Credential hygiene procedure note",
  category: "procedures",
  source: "session 2026-07-26",
};

function pad(content) {
  // Enough words to clear the short-content quality floor.
  return `${content} — documented so the quality score clears the short-content floor for this gate test.`;
}

function assertHasTail(spans, expected, label = "") {
  const ok =
    typeof expected === "string"
      ? spans.some((s) => s.tail === expected || s.tail.includes(expected))
      : spans.some((s) => expected.test(s.tail));
  assert.ok(ok, `${label} missing tail ${String(expected)} in ${JSON.stringify(spans)}`);
}

test("findInconclusiveHighRiskAssignments catches unquoted multi-word tails (#147)", () => {
  const spans = findInconclusiveHighRiskAssignments(
    "password: correct horse battery staple is what was pasted",
  );
  assert.ok(spans.length >= 1);
  assert.equal(spans[0].keyword.toLowerCase(), "password");
  assertHasTail(spans, "correct horse battery staple");
  // Tail must include the passphrase; quality-floor padding may also appear as
  // a separate candidate (AFM blocks if ANY span is credential).
  const padded = findInconclusiveHighRiskAssignments(pad("password: correct horse battery staple"));
  assertHasTail(padded, "correct horse battery staple");
});

test("findInconclusiveHighRiskAssignments splits same-line assignments (no swallow)", () => {
  const spans = findInconclusiveHighRiskAssignments(
    "password: use a manager and secret: correct horse battery staple",
  );
  assert.ok(spans.some((s) => s.keyword.toLowerCase() === "password"));
  assert.ok(spans.some((s) => s.keyword.toLowerCase() === "secret"));
  assertHasTail(spans, "correct horse battery staple");
});

test("findInconclusiveHighRiskAssignments ignores prose-shaped single tokens and quoted values", () => {
  assert.deepEqual(findInconclusiveHighRiskAssignments("password: hunter2"), []);
  // Well-formed quoted 8+ literal is regex-tier territory, not inconclusive.
  assert.deepEqual(
    findInconclusiveHighRiskAssignments('password: "correct horse battery staple"'),
    [],
  );
  // First token ≥8 already matches HIGH_RISK — not inconclusive.
  assert.deepEqual(
    findInconclusiveHighRiskAssignments("password: correcthorsebatterystaple"),
    [],
  );
});

test("malformed / short quotes do not create a regex+AFM blind spot", () => {
  assertHasTail(
    findInconclusiveHighRiskAssignments('password: "correct horse battery staple'),
    "correct horse battery staple",
  );
  assertHasTail(
    findInconclusiveHighRiskAssignments('password: "correcthorsebatterystaple'),
    "correcthorsebatterystaple",
  );
  assertHasTail(findInconclusiveHighRiskAssignments('password: "my dogs"'), "my dogs");
  assertHasTail(
    findInconclusiveHighRiskAssignments('password: "x" correct horse battery staple'),
    "correct horse battery staple",
  );
  assertHasTail(
    findInconclusiveHighRiskAssignments('password: "x" correcthorsebatterystaple'),
    "correcthorsebatterystaple",
  );
  const nested = findInconclusiveHighRiskAssignments(
    'password: "x" "correcthorsebatterystaple"',
  );
  assertHasTail(nested, "correcthorsebatterystaple");
  assert.equal(detectSecretMaterial('password: "x" "correcthorsebatterystaple"'), null);
});

test("English apostrophes are not treated as quote delimiters", () => {
  const spans = findInconclusiveHighRiskAssignments(
    "password: don't share this passphrase ever",
  );
  assert.ok(spans.length >= 1, JSON.stringify(spans));
  assert.ok(spans.some((s) => /don'?t share this passphrase/.test(s.tail)));
});

test("leading single-quote opaques surface like double-quote repairs", () => {
  assertHasTail(
    findInconclusiveHighRiskAssignments("password: 'correcthorsebatterystaple"),
    "correcthorsebatterystaple",
  );
  assertHasTail(
    findInconclusiveHighRiskAssignments("password: 'x'correcthorsebatterystaple"),
    "correcthorsebatterystaple",
  );
  assertHasTail(
    findInconclusiveHighRiskAssignments("password: ''abcdefghijklmn"),
    "abcdefghijklmn",
  );
});

test("hybrid / nested quotes that regex cannot own still surface for AFM", () => {
  const cases = [
    `password: "'correct horse battery staple'"`,
    `password: '"correct horse battery staple"'`,
    `password: "'correcthorsebatterystaple'"`,
    `password: "correct'horsebatterystaple"`,
    `password: 'correct"horsebatterystaple'`,
    `password: "don'tusethispass"`,
    `secret: "user's private passphrase here"`,
  ];
  for (const content of cases) {
    assert.equal(detectSecretMaterial(content), null, `regex miss: ${content}`);
    const spans = findInconclusiveHighRiskAssignments(content);
    assert.ok(spans.length >= 1, `AFM span required: ${content} → ${JSON.stringify(spans)}`);
  }
});

test("quoted secrets and after-secrets both reach AFM (no preference war)", () => {
  assertHasTail(
    findInconclusiveHighRiskAssignments(pad(`password: "'correct horse battery staple'"`)),
    "correct horse battery staple",
  );
  assertHasTail(
    findInconclusiveHighRiskAssignments(
      'password: "don\'tusethispass" stored in the keychain after rotation notes',
    ),
    "don'tusethispass",
  );
  assertHasTail(
    findInconclusiveHighRiskAssignments(
      'password: "don\'tusethispass" is stored in the keychain after rotation notes',
    ),
    "don'tusethispass",
  );
  assertHasTail(
    findInconclusiveHighRiskAssignments(`password: "'correct horse battery staple'".`),
    "correct horse battery staple",
  );
});

test("triple-quoted and backslash-escaped opaques surface for AFM", () => {
  for (const content of [
    `password: """correcthorsebatterystaple"""`,
    `password: """correct horse battery staple"""`,
    `password: '''correcthorsebatterystaple'''`,
    `password: \\"correcthorsebatterystaple`,
    String.raw`password: \\"correcthorsebatterystaple`,
    `password: ""correct horse battery staple""`,
  ]) {
    assert.equal(detectSecretMaterial(content), null, content);
    const spans = findInconclusiveHighRiskAssignments(content);
    assert.ok(spans.some((s) => /correct/.test(s.tail)), `${content} → ${JSON.stringify(spans)}`);
    assert.ok(spans.every((s) => !/[\"']$/.test(s.tail)), JSON.stringify(spans));
  }
});

test("short multi-token decoy quotes do not hide the real after-secret", () => {
  const cases = [
    'password: "my dog" correct horse battery staple',
    'password: "my dog" is correct horse battery staple',
    'password: "my dog" — correct horse battery staple',
    'password: "my dog"—correct horse battery staple',
    'password: "my dog". correct horse battery staple',
    'password: "my dog" after correct horse battery staple',
    'password: "my dog" stored correct horse battery staple',
    'password: "my dog" documented correct horse battery staple',
    // Unquoted prose — passphrase (em-dash must not drop the right side).
    "password: use a manager — correct horse battery staple",
    // Newline-split assignment.
    "password: use a manager\ncorrect horse battery staple",
    "password: use a manager\r\ncorrect horse battery staple",
    "password: use a manager\ncorrecthorsebatterystaple",
    'password: use a manager\n"correct horse battery staple"',
    'password: "x"\ncorrecthorsebatterystaple',
  ];
  for (const content of cases) {
    assertHasTail(
      findInconclusiveHighRiskAssignments(content),
      /correct/,
      content,
    );
  }
});

test("credential warning lists keywords from all inconclusive spans", async () => {
  const report = await assessLearningQualityAsync(
    {
      ...GOOD_INPUT,
      content: pad("password: use a manager and secret: correct horse battery staple"),
    },
    { classifyInconclusive: async () => "credential" },
  );
  assert.equal(report.ok, false);
  assert.ok(
    report.summary.includes("password") && report.summary.includes("secret"),
    report.summary,
  );
});

test("regex tier still misses unquoted multi-word passphrases (AFM owns this case)", () => {
  assert.equal(detectSecretMaterial("password: correct horse battery staple"), null);
  assert.equal(detectSecretMaterial("password: use a manager for storage"), null);
  const sync = assessLearningQuality({
    ...GOOD_INPUT,
    content: pad("password: correct horse battery staple"),
  });
  assert.equal(sync.ok, true, "sync regex path must stay inconclusive");
});

test("word-boundary: keyword must not match as a substring of a longer word (#191)", () => {
  // `apassword:` / `notasecret:` embed the keyword but are not the keyword —
  // the discovery regex must require a boundary, same as its nextAssignRe
  // sibling.
  assert.deepEqual(
    findInconclusiveHighRiskAssignments("apassword: correct horse battery staple"),
    [],
  );
  assert.deepEqual(
    findInconclusiveHighRiskAssignments("notasecret: correct horse battery staple"),
    [],
  );
  // A real, boundary-delimited keyword still surfaces as inconclusive.
  assertHasTail(
    findInconclusiveHighRiskAssignments("password: correct horse battery staple"),
    "correct horse battery staple",
  );
  assertHasTail(
    findInconclusiveHighRiskAssignments("my secret: correct horse battery staple"),
    "correct horse battery staple",
  );
});

test("smart/curly-quoted decoys split like ASCII decoys, not one glued blob (#191)", () => {
  const asciiSpans = findInconclusiveHighRiskAssignments(
    'password: "my dog" correct horse battery staple',
  );
  const doubleCurlySpans = findInconclusiveHighRiskAssignments(
    "password: “my dog” correct horse battery staple",
  );
  const singleCurlySpans = findInconclusiveHighRiskAssignments(
    "password: ‘my dog’ correct horse battery staple",
  );
  for (const spans of [asciiSpans, doubleCurlySpans, singleCurlySpans]) {
    // Real passphrase must be its own candidate, not only reachable inside a
    // glued "decoy + passphrase" blob.
    assert.ok(
      spans.some((s) => s.tail === "correct horse battery staple"),
      JSON.stringify(spans),
    );
    // Decoy quote content must also surface as its own candidate.
    assert.ok(
      spans.some((s) => s.tail === "my dog"),
      JSON.stringify(spans),
    );
  }
});

test("#138 vocabulary notes are not inconclusive spans", () => {
  const notes = [
    "PyPI trusted publishing requires the GitHub Actions permission id-token: write, and the publisher registration must match the lowercase OIDC repository claim.",
    "The tokenizer budget is 4096 tokens per context window; tiktoken counts them differently than the API token_count field reports.",
    "Never store the api key in the vault; the old token was revoked after the incident and rotation is documented in the runbook.",
    "Secret handling procedure: secrets belong in the keychain, never in memory notes; password rotation happens quarterly.",
  ];
  for (const content of notes) {
    assert.equal(detectSecretMaterial(content), null, content.slice(0, 60));
    assert.deepEqual(findInconclusiveHighRiskAssignments(content), [], content.slice(0, 60));
  }
});

test("AFM inconclusive tier blocks credential verdict (#147)", async () => {
  const report = await assessLearningQualityAsync(
    { ...GOOD_INPUT, content: pad("password: correct horse battery staple") },
    { classifyInconclusive: async () => "credential" },
  );
  assert.equal(report.ok, false);
  assert.ok(report.warnings.some((w) => w.includes("sensitive material")));
  assert.ok(report.warnings.some((w) => w.includes("unquoted multi-word")));
  // Must not echo the passphrase into the warning text.
  assert.ok(!report.summary.includes("correct horse battery staple"));
  assert.equal(report.semanticTier, "ran");
});

test("AFM inconclusive tier allows prose verdict (#147)", async () => {
  const report = await assessLearningQualityAsync(
    { ...GOOD_INPUT, content: pad("password: use a manager for all credentials") },
    { classifyInconclusive: async () => "prose" },
  );
  assert.equal(report.ok, true, report.summary);
  // #237 / SEC-G6: "examined and cleared" is observable, not just fail-open.
  assert.equal(report.semanticTier, "ran");
});

test("AFM unavailable fails open (enhancement tier, not replacement)", async () => {
  const report = await assessLearningQualityAsync(
    { ...GOOD_INPUT, content: pad("password: correct horse battery staple") },
    { classifyInconclusive: async () => "unavailable" },
  );
  assert.equal(report.ok, true, "unavailable must not hard-block");
  assert.equal(report.semanticTier, "unavailable");
});

test("AFM classifier throw fails open", async () => {
  const report = await assessLearningQualityAsync(
    { ...GOOD_INPUT, content: pad("secret: red blue green yellow") },
    {
      classifyInconclusive: async () => {
        throw new Error("afm exploded");
      },
    },
  );
  assert.equal(report.ok, true);
  assert.equal(report.semanticTier, "unavailable");
});

test("semanticTier distinguishes prose-cleared from unavailable/throw (#237 SEC-G6)", async () => {
  // Reproduction from issue #237: same input, four injected verdicts — three
  // non-blocking outcomes were previously byte-identical in every field.
  const input = {
    title: "Ops runbook for the prod cluster",
    content: pad(
      "For the staging box the password: correct horse battery staple was rotated on Tuesday by the on-call.",
    ),
    category: "ops",
    source: "session",
  };

  const credential = await assessLearningQualityAsync(input, {
    classifyInconclusive: async () => "credential",
  });
  const prose = await assessLearningQualityAsync(input, {
    classifyInconclusive: async () => "prose",
  });
  const unavailable = await assessLearningQualityAsync(input, {
    classifyInconclusive: async () => "unavailable",
  });
  const threw = await assessLearningQualityAsync(input, {
    classifyInconclusive: async () => {
      throw new Error("classifier boom");
    },
  });

  assert.equal(credential.ok, false);
  assert.equal(credential.semanticTier, "ran");

  assert.equal(prose.ok, true);
  assert.equal(prose.semanticTier, "ran");

  assert.equal(unavailable.ok, true);
  assert.equal(unavailable.semanticTier, "unavailable");

  assert.equal(threw.ok, true);
  assert.equal(threw.semanticTier, "unavailable");

  // The defect was that prose / unavailable / throw were indistinguishable.
  // ok+score alone still match for fail-open paths; semanticTier must differ.
  assert.equal(prose.ok, unavailable.ok);
  assert.equal(prose.score, unavailable.score);
  assert.notEqual(
    prose.semanticTier,
    unavailable.semanticTier,
    "prose-cleared must not look identical to unavailable in the audit field",
  );
  assert.equal(unavailable.semanticTier, threw.semanticTier);
});

test("AFM tier is skipped when regex already hard-blocked", async () => {
  let called = 0;
  const report = await assessLearningQualityAsync(
    {
      ...GOOD_INPUT,
      content: pad('password: "correcthorsebatterystaple"'),
    },
    {
      classifyInconclusive: async () => {
        called += 1;
        return "prose";
      },
    },
  );
  assert.equal(report.ok, false);
  assert.equal(called, 0, "must not call AFM after regex hard-block");
  assert.equal(report.semanticTier, "skipped");
});

test("AFM tier is skipped when no inconclusive spans exist", async () => {
  let called = 0;
  const report = await assessLearningQualityAsync(
    {
      ...GOOD_INPUT,
      content: pad("Never store secrets in the vault; rotate quarterly."),
    },
    {
      classifyInconclusive: async () => {
        called += 1;
        return "credential";
      },
    },
  );
  assert.equal(report.ok, true);
  assert.equal(called, 0);
  assert.equal(report.semanticTier, "skipped");
});

test("sync assessLearningQuality always reports semanticTier skipped", () => {
  const report = assessLearningQuality({
    ...GOOD_INPUT,
    content: pad("password: correct horse battery staple"),
  });
  assert.equal(report.semanticTier, "skipped");
});

test("same-line buried passphrase still reaches AFM as its own span", async () => {
  const seen = [];
  const report = await assessLearningQualityAsync(
    {
      ...GOOD_INPUT,
      content: pad("password: use a manager and secret: correct horse battery staple"),
    },
    {
      classifyInconclusive: async (spans) => {
        seen.push(...spans.map((s) => `${s.keyword}:${s.tail}`));
        // Mimic AFM: any credential-shaped span → block.
        return spans.some((s) => /horse battery/.test(s.tail)) ? "credential" : "prose";
      },
    },
  );
  assert.ok(seen.some((s) => s.startsWith("secret:")), seen);
  assert.equal(report.ok, false);
});

test("parseInconclusiveAfmVerdict normalizes case and chat/native shapes", () => {
  assert.equal(parseInconclusiveAfmVerdict({ verdict: "Credential" }), "credential");
  assert.equal(parseInconclusiveAfmVerdict({ verdict: " PROSE " }), "prose");
  assert.equal(parseInconclusiveAfmVerdict({ verdict: "maybe" }), undefined);
  assert.equal(
    parseInconclusiveAfmVerdict({
      choices: [{ message: { content: '{"verdict":"credential"}' } }],
    }),
    "credential",
  );
  assert.equal(
    parseInconclusiveAfmVerdict({
      choices: [{ message: { content: "```json\n{\"verdict\":\"prose\"}\n```" } }],
    }),
    "prose",
  );
  assert.equal(parseInconclusiveAfmVerdict({ answer: '{"verdict":"credential"}' }), "credential");
});

test("classifyInconclusiveWithAfmImpl: off / ok:false / malformed → unavailable", async () => {
  assert.equal(
    await classifyInconclusiveWithAfmImpl(
      [{ keyword: "password", tail: "correct horse battery staple" }],
      { providerMode: "off" },
    ),
    "unavailable",
  );
  assert.equal(
    await classifyInconclusiveWithAfmImpl(
      [{ keyword: "password", tail: "correct horse battery staple" }],
      {
        providerMode: "bridge",
        callAfm: async () => ({ ok: false, error: "down" }),
      },
    ),
    "unavailable",
  );
  assert.equal(
    await classifyInconclusiveWithAfmImpl(
      [{ keyword: "password", tail: "correct horse battery staple" }],
      {
        providerMode: "bridge",
        callAfm: async () => ({ ok: true, data: { verdict: "huh" } }),
      },
    ),
    "unavailable",
  );
});

test("classifyInconclusiveWithAfmImpl: credential and prose via mocked callAfm", async () => {
  assert.equal(
    await classifyInconclusiveWithAfmImpl(
      [{ keyword: "password", tail: "correct horse battery staple" }],
      {
        providerMode: "bridge",
        callAfm: async () => ({
          ok: true,
          data: { choices: [{ message: { content: '{"verdict":"Credential"}' } }] },
        }),
      },
    ),
    "credential",
  );
  assert.equal(
    await classifyInconclusiveWithAfmImpl(
      [{ keyword: "password", tail: "use a manager" }],
      {
        providerMode: "native",
        callAfm: async () => ({ ok: true, data: { answer: '{"verdict":"prose"}' } }),
      },
    ),
    "prose",
  );
});
