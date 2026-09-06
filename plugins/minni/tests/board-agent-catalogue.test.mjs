import assert from 'node:assert/strict';
import test from 'node:test';
import { mapAgents, filterAgentCatalogue } from './.compiled/board-test.mjs';

const agents = mapAgents([
  { id: 'opaque-history', registered: false, registrationKnown: true, vault: '/vault/old' },
  { id: 'codex', displayName: 'Codex', registered: true, registrationKnown: true, description: 'Coding assistant', vault: '/vault/codex' },
  { id: 'unresolved', registered: false, registrationKnown: false, vault: '/vault/unknown' },
]);
test('catalogue separates unregistered records from unknown registration', () => {
  assert.deepEqual(filterAgentCatalogue(agents, 'registered', '').map(x => x.id), ['codex']);
  assert.deepEqual(filterAgentCatalogue(agents, 'unregistered', '').map(x => x.id), ['opaque-history']);
  assert.deepEqual(filterAgentCatalogue(agents, 'unknown', '').map(x => x.id), ['unresolved']);
  assert.equal(filterAgentCatalogue(agents, 'all', '').length, 3);
});
test('catalogue search uses actual names descriptions and storage paths within selected scope', () => {
  for (const query of [' CODEX ', 'coding assistant', '/vault/codex']) {
    assert.deepEqual(filterAgentCatalogue(agents, 'all', query).map(x => x.id), ['codex']);
  }
  assert.equal(filterAgentCatalogue(agents, 'unregistered', 'codex').length, 0);
  assert.equal(filterAgentCatalogue(agents, 'all', 'nonexistent').length, 0);
  assert.equal(agents.length, 3);
});
