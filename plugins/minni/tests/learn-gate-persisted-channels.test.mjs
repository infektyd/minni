import assert from "node:assert/strict";
import test from "node:test";

import { assessLearningQuality, assessLearningQualityAsync } from "../dist/policy.js";

// The learn gate scanned `content` only, while `minni_learn` persists title,
// content, category and source into the vault note. A credential pasted into
// any of the other three channels was written to disk unscanned.
//
// Fixture "secrets" are split-and-joined so file-level scanners (GitHub push
// protection, the repo's own Forbidden Files gate) do not flag this file.
const j = (...parts) => parts.join("");
const FAKE_PAT = j("ghp_", "AbCdEf0123456789AbCdEf0123456789AbCd");

const CLEAN = {
  title: "Rotation runbook for the staging publisher",
  content:
    "The staging publisher credential is rotated quarterly by the on-call engineer and the rotation is recorded in the incident log.",
  category: "procedures",
  source: "session 2026-08-01",
};

function blocked(report) {
  return report.ok === false && report.warnings.some((w) => w.includes("sensitive material"));
}

test("a credential in ANY persisted channel blocks the learn gate", () => {
  assert.equal(blocked(assessLearningQuality(CLEAN)), false, "control note must pass");

  for (const field of ["title", "content", "category", "source"]) {
    const report = assessLearningQuality({ ...CLEAN, [field]: `${CLEAN[field]} ${FAKE_PAT}` });
    assert.ok(blocked(report), `credential in "${field}" must block: ${report.summary}`);
    assert.ok(
      report.warnings.some((w) => w.includes(field)),
      `the warning must name the offending field "${field}": ${report.summary}`,
    );
  }
});

test("the warning never echoes the credential value back to the caller", () => {
  for (const field of ["title", "content", "category", "source"]) {
    const report = assessLearningQuality({ ...CLEAN, [field]: `${CLEAN[field]} ${FAKE_PAT}` });
    assert.ok(
      !report.summary.includes(FAKE_PAT),
      `summary must not echo the detected credential (${field})`,
    );
  }
});

// The #147 AFM inconclusive tier ran over `content` only, so an unquoted
// multi-word passphrase in the title never reached the semantic classifier.
test("the AFM inconclusive tier sees high-risk spans from every persisted channel", async () => {
  for (const field of ["title", "content", "category", "source"]) {
    const seen = [];
    const report = await assessLearningQualityAsync(
      { ...CLEAN, [field]: `${CLEAN[field]} password: correct horse battery staple` },
      {
        classifyInconclusive: async (spans) => {
          seen.push(...spans);
          return "credential";
        },
      },
    );
    assert.ok(seen.length > 0, `no span reached the classifier from "${field}"`);
    assert.ok(blocked(report), `AFM "credential" verdict on "${field}" must block: ${report.summary}`);
  }
});

// Guard the #138 trade: the gate flags credential MATERIAL, not credential
// VOCABULARY. Widening the scan must not start blocking ordinary titles.
test("credential vocabulary in title/category/source still passes", () => {
  const notes = [
    { ...CLEAN, title: "Never store the api key or token in the vault" },
    { ...CLEAN, category: "secret-management" },
    { ...CLEAN, source: "docs/contracts/POLICY.md password rotation section" },
    { ...CLEAN, title: "GitHub Actions id-token: write permission for trusted publishing" },
  ];
  for (const note of notes) {
    const report = assessLearningQuality(note);
    assert.equal(blocked(report), false, `vocabulary must not block: ${report.summary}`);
  }
});
