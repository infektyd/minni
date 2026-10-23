// Parity gate for the harness-envelope TEXT rules (recall-to-use rev5, 5.2.1).
//
// The TypeScript rules (src/harness-envelope-rules.ts) and their Python mirror
// (src/minni/afm_passes/harness_envelopes.py) share one synthetic case file,
// tests/fixtures/harness_envelope_cases.json. tests/test_harness_envelope_parity.py
// runs the same file on the Python side.
//
// Two directions are pinned, so a rule added on only one side fails a gate:
//   1. every rule_id in this side's constants has at least one case;
//   2. every rule_id a case expects exists in this side's constants.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  HARNESS_ENVELOPE_RULES,
  harnessEnvelopeRuleIds,
} from "../dist/harness-envelope-rules.js";
import {
  classifyPromptEnvelope,
  stripHarnessBlocks,
} from "../dist/harness-envelope.js";

const CASES_URL = new URL("../../../tests/fixtures/harness_envelope_cases.json", import.meta.url);
const cases = JSON.parse(await readFile(CASES_URL, "utf8"));

test("case file is versioned and non-trivial", () => {
  assert.equal(cases.version, 1);
  assert.ok(cases.strip_cases.length >= 10);
  assert.ok(cases.classify_cases.length >= 30);
  const ids = [...cases.strip_cases, ...cases.classify_cases].map((c) => c.id);
  assert.equal(new Set(ids).size, ids.length, "case ids must be unique");
});

for (const c of cases.strip_cases) {
  test(`strip ${c.id}: ${c.note}`, () => {
    assert.equal(stripHarnessBlocks(c.input), c.expected);
  });
}

for (const c of cases.classify_cases) {
  test(`classify ${c.id}: ${c.note}`, () => {
    const got = classifyPromptEnvelope(c.input);
    assert.deepEqual(
      {
        kind: got.kind,
        subkind: got.subkind,
        rule_id: got.rule_id ?? null,
        body: got.body,
        suspicious_prefix: got.suspicious_prefix === true,
      },
      c.expected,
    );
    // Without a ctx the template rule is off and nothing is evaluated.
    assert.equal(got.template_rule, "off");
    assert.equal(got.template, undefined);
  });
}

test("every rule_id in the TypeScript constants has at least one case", () => {
  const expectedIds = new Set(cases.classify_cases.map((c) => c.expected.rule_id));
  const missing = harnessEnvelopeRuleIds().filter((id) => !expectedIds.has(id));
  assert.deepEqual(missing, [], `rules with no case: ${missing.join(", ")}`);
});

test("every rule_id a case expects exists in the TypeScript constants", () => {
  const known = new Set(harnessEnvelopeRuleIds());
  const unknown = cases.classify_cases
    .map((c) => c.expected.rule_id)
    .filter((id) => id !== null && !known.has(id));
  assert.deepEqual(unknown, [], `cases naming unknown rules: ${unknown.join(", ")}`);
});

test("rule ids are unique and stable-shaped", () => {
  const ids = harnessEnvelopeRuleIds();
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ids) assert.match(id, /^(block|skip|wrap)\.[a-z_]+$/);
  assert.equal(
    ids.length,
    HARNESS_ENVELOPE_RULES.blockTags.length +
      HARNESS_ENVELOPE_RULES.skip.length +
      HARNESS_ENVELOPE_RULES.wrappers.length,
  );
});

test("the template rule id is not part of the shared text-rule set", () => {
  assert.ok(!harnessEnvelopeRuleIds().includes(HARNESS_ENVELOPE_RULES.templateRule.rule_id));
});
