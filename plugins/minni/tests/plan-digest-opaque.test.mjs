import assert from 'node:assert/strict';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {computePlanDigest, persistPlan, rehydratePlan} from '../dist/plan.js';

const cases = [
  [0, 'a5946fe61e1eeafe'],
  [369, '2169255635901885'],
  [5675, '0261962331906577'],
  [12787, '9033726326539107'],
];
for (const [seed, digest] of cases) {
  test(`opaque plan digest round trips exact lexical value ${digest}`, async t => {
    const vaultPath = await mkdtemp(path.join(tmpdir(), 'minni-digest-scalar-'));
    t.after(() => rm(vaultPath, {recursive: true, force: true}));
    const plan = {
      plan_id: 'plan-opaque-digest', goal: `Opaque digest fixture ${seed}`,
      status: 'draft', next_action: 'Start', constraints: [], open_questions: [],
      scar_tissue: [], slices: [{id: 's0', title: 'Fixture', status: 'pending'}],
      created: '2026-09-06T00:00:00.000Z', updated: '2026-09-06T00:00:00.000Z',
      rev: 0, plan_digest: '',
    };
    assert.equal(computePlanDigest(plan), digest);
    const {notePath} = await persistPlan(plan, {vaultPath});
    const raw = await readFile(notePath, 'utf8');
    for (const scalar of [digest, JSON.stringify(digest), `'${digest}'`]) {
      const fixture = raw.replace(/^plan_digest:.*$/m, `plan_digest: ${scalar}`);
      await writeFile(notePath, fixture);
      const read = await rehydratePlan(notePath);
      assert.equal(read.plan_digest, digest);
      assert.equal(await readFile(notePath, 'utf8'), fixture, 'reading existing notes must not rewrite them');
    }
    for (const forged of ['4217204359752419', '0000000000000000', '9999999999999999', '1e12345678901234']) {
      await writeFile(notePath, raw.replace(/^plan_digest:.*$/m, `plan_digest: ${forged}`));
      await assert.rejects(rehydratePlan(notePath), error => {
        assert.match(error.message, /plan_digest mismatch/);
        assert.ok(error.message.includes(`stored=${forged} `), 'compare the original lexeme, never a rounded number');
        return true;
      });
    }
  });
}
