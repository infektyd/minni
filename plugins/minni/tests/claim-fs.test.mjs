import assert from 'node:assert/strict';
import { constants } from 'node:fs';
import * as native from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { build } from 'esbuild';
import * as adapter from '../dist/claim-fs.js';

const DIR = constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW;
const READ = constants.O_RDONLY | constants.O_NOFOLLOW;
const WRITE = constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW;
const hash = 'a'.repeat(32), filename = `${'b'.repeat(32)}.json`;
async function fixture(t, api = adapter) {
  const vault = await native.mkdtemp(path.join(tmpdir(), 'minni-claim-fs-'));
  const root = await api.open(vault, DIR);
  const handles = [root];
  t.after(async () => {
    for (const h of handles.reverse()) await h.close().catch(() => {});
    await api.closeClaimFs(root);
    await native.rm(vault, { recursive: true, force: true });
  });
  const alias = await api.startClaimFs(root);
  let parent = root;
  for (const name of ['.runtime', 'thread-claims', hash]) {
    const p = `${alias}/${parent.fd}/${name}`;
    await api.mkdir(p, { mode: 0o700 });
    parent = await api.open(p, DIR); handles.push(parent);
  }
  return { vault, alias, root, parent, api, at: name => `${alias}/${parent.fd}/${name}` };
}

test('claim adapter writes atomically through inherited descriptors and rejects expired capabilities', async t => {
  const f = await fixture(t);
  const temp = `.${'b'.repeat(32)}.${'c'.repeat(32)}.tmp`;
  const h = await adapter.open(f.at(temp), WRITE, 0o600);
  await h.writeFile('{"claim":"private"}\n', 'utf8'); await h.sync(); await h.close();
  await adapter.rename(f.at(temp), f.at(filename));
  const read = await adapter.open(f.at(filename), READ);
  assert.equal(await read.readFile('utf8'), '{"claim":"private"}\n');
  assert.equal((await read.stat()).mode & 0o777, 0o600);
  await read.close();
  await adapter.closeClaimFs(f.root);
  await assert.rejects(adapter.open(f.at(filename), READ), { code: 'EBADF' });
});

test('claim helper rejects symlinks, hardlinks, wrong modes, oversize writes and invalid child grammar', async t => {
  const f = await fixture(t);
  const real = path.join(f.vault, '.runtime', 'thread-claims', hash);
  const outside = path.join(f.vault, 'outside');
  await native.writeFile(outside, 'never follow', { mode: 0o600 });
  await native.symlink(outside, path.join(real, filename));
  await assert.rejects(adapter.open(f.at(filename), READ), e => ['ELOOP', 'EMLINK'].includes(e.code));
  await native.unlink(path.join(real, filename));
  await native.link(outside, path.join(real, filename));
  let h = await adapter.open(f.at(filename), READ);
  await assert.rejects(h.readFile('utf8'), { code: 'EPERM' }); await h.close();
  await native.unlink(path.join(real, filename));
  await native.writeFile(path.join(real, filename), 'private', { mode: 0o640 });
  h = await adapter.open(f.at(filename), READ);
  await assert.rejects(h.readFile('utf8'), { code: 'EPERM' }); await h.close();
  const temp = `.${'b'.repeat(32)}.${'d'.repeat(32)}.tmp`;
  h = await adapter.open(f.at(temp), WRITE, 0o600);
  await assert.rejects(h.writeFile('x'.repeat(65537), 'utf8'), { code: 'EFBIG' }); await h.close();
  await assert.rejects(adapter.mkdir(`${f.alias}/${f.root.fd}/unexpected`, { mode: 0o700 }), { code: 'EINVAL' });
  await assert.rejects(adapter.open(f.at('../outside'), READ), { code: 'EINVAL' });
  assert.equal(await native.readFile(outside, 'utf8'), 'never follow');
});

test('bundled claim adapter carries helper without source checkout or Python modules', async t => {
  const dir = await native.mkdtemp(path.join(tmpdir(), 'minni-claim-bundle-'));
  t.after(() => native.rm(dir, { recursive: true, force: true }));
  const out = path.join(dir, 'adapter.mjs');
  await build({ entryPoints: [new URL('../dist/claim-fs.js', import.meta.url).pathname], outfile: out, bundle: true, platform: 'node', format: 'esm', logLevel: 'silent' });
  const bundled = await import(pathToFileURL(out));
  const f = await fixture(t, bundled);
  assert.ok((await f.parent.stat()).isDirectory());
});

test('missing interpreter fails promptly and reaps failed helper', async t => {
  const before = process.env.MINNI_CLAIM_PYTHON;
  process.env.MINNI_CLAIM_PYTHON = '/nonexistent/minni-claim-python';
  t.after(() => { if (before === undefined) delete process.env.MINNI_CLAIM_PYTHON; else process.env.MINNI_CLAIM_PYTHON = before; });
  const dir = await native.mkdtemp(path.join(tmpdir(), 'minni-claim-missing-'));
  const root = await adapter.open(dir, DIR);
  t.after(async () => { await root.close(); await native.rm(dir, { recursive: true, force: true }); });
  await assert.rejects(adapter.startClaimFs(root), { code: 'EHELPER' });
});

test('unresponsive interpreter is killed and reaped at the request deadline', { timeout: 15000 }, async t => {
  const dir = await native.mkdtemp(path.join(tmpdir(), 'minni-claim-timeout-'));
  const executable = path.join(dir, 'python');
  const pidFile = path.join(dir, 'pid');
  // exec replaces the shell, so this probe cannot orphan a sleep descendant.
  const code = `require('fs').writeFileSync(${JSON.stringify(pidFile)},String(process.pid));setInterval(()=>{},1000)`;
  const quote = s => `'${s.replaceAll("'", "'\\''")}'`;
  await native.writeFile(executable, `#!/bin/sh\nexec ${quote(process.execPath)} -e ${quote(code)}\n`, { mode: 0o700 });
  const before = process.env.MINNI_CLAIM_PYTHON;
  process.env.MINNI_CLAIM_PYTHON = executable;
  const root = await adapter.open(dir, DIR);
  t.after(async () => { if (before === undefined) delete process.env.MINNI_CLAIM_PYTHON; else process.env.MINNI_CLAIM_PYTHON = before; await adapter.closeClaimFs(root); await root.close(); await native.rm(dir, { recursive: true, force: true }); });
  await assert.rejects(adapter.startClaimFs(root), { code: 'ETIMEDOUT' });
  const pid = Number(await native.readFile(pidFile, 'utf8'));
  assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
});

test('startup, malformed protocol, and abrupt helper exits reject and leave no child', async t => {
  for (const kind of ['startup-exit', 'malformed', 'abrupt-after-ready']) {
    await t.test(kind, async st => {
      const dir = await native.mkdtemp(path.join(tmpdir(), 'minni-claim-death-'));
      const executable = path.join(dir, 'python');
      const pidFile = path.join(dir, 'pid');
      const prelude = `const fs=require('fs');fs.writeFileSync(${JSON.stringify(pidFile)},String(process.pid));`;
      const body = kind === 'startup-exit' ? 'process.exit(42)'
        : kind === 'malformed' ? "process.stdout.write('invalid-json\\n');setInterval(()=>{},1000)"
          : "process.stdin.once('data',b=>{const q=JSON.parse(b);const s=fs.fstatSync(3);process.stdout.write(JSON.stringify({id:q.id,result:{dev:s.dev,ino:s.ino}})+'\\n');setTimeout(()=>process.exit(42),30)});";
      const quote = s => `'${s.replaceAll("'", "'\\''")}'`;
      await native.writeFile(executable, `#!/bin/sh\nexec ${quote(process.execPath)} -e ${quote(prelude + body)}\n`, { mode: 0o700 });
      const before = process.env.MINNI_CLAIM_PYTHON;
      process.env.MINNI_CLAIM_PYTHON = executable;
      const root = await adapter.open(dir, DIR);
      st.after(async () => { if (before === undefined) delete process.env.MINNI_CLAIM_PYTHON; else process.env.MINNI_CLAIM_PYTHON = before; await adapter.closeClaimFs(root); await root.close(); await native.rm(dir, { recursive: true, force: true }); });
      if (kind === 'abrupt-after-ready') {
        const alias = await adapter.startClaimFs(root);
        await new Promise(resolve => setTimeout(resolve, 60));
        await assert.rejects(adapter.mkdir(`${alias}/${root.fd}/.runtime`, { mode: 0o700 }), { code: 'EPIPE' });
        await adapter.closeClaimFs(root);
      } else {
        await assert.rejects(adapter.startClaimFs(root), { code: kind === 'malformed' ? 'EPROTO' : 'EPIPE' });
      }
      const pid = Number(await native.readFile(pidFile, 'utf8'));
      assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
    });
  }
});

test('mutation scopes reuse helpers across closed roots and nested/concurrent calls, then reap on error', async t => {
  const childProcess = (await import('node:child_process')).default;
  const { syncBuiltinESMExports } = await import('node:module');
  const spawn = childProcess.spawn;
  const children = [];
  childProcess.spawn = (...args) => {
    const child = spawn(...args); children.push(child); return child;
  };
  syncBuiltinESMExports();
  const dir = await native.mkdtemp(path.join(tmpdir(), 'minni-claim-scope-'));
  t.after(async () => { childProcess.spawn = spawn; syncBuiltinESMExports(); await native.rm(dir, { recursive: true, force: true }); });
  let first;
  await assert.rejects(adapter.withClaimFsScope(async () => {
    const h = await adapter.open(dir, DIR);
    first = await adapter.startClaimFs(h);
    await adapter.closeClaimFs(h); await h.close();
    assert.equal(children.length, 1);
    // Occupy the now-closed native FD: reuse must translate root identities,
    // not assume the second Node descriptor number equals the helper's key.
    const occupied = await native.open('/dev/null', 'r');
    try {
      await adapter.withClaimFsScope(async () => {
        const roots = await Promise.all([adapter.open(dir, DIR), adapter.open(dir, DIR)]);
        try {
          const aliases = await Promise.all(roots.map(root => adapter.startClaimFs(root)));
          assert.deepEqual(aliases, [first, first]);
          await adapter.mkdir(`${first}/${adapter.claimHandleId(roots[0])}/.runtime`, { mode: 0o700 });
        } finally {
          for (const root of roots) { await adapter.closeClaimFs(root); await root.close(); }
        }
      });
    } finally { await occupied.close(); }
    assert.equal(children.length, 1);
    throw new Error('abort mutation');
  }), /abort mutation/);
  assert.throws(() => process.kill(children[0].pid, 0), { code: 'ESRCH' });
  await adapter.withClaimFsScope(async () => {
    const root = await adapter.open(dir, DIR);
    try { assert.notEqual(await adapter.startClaimFs(root), first); }
    finally { await adapter.closeClaimFs(root); await root.close(); }
  });
  assert.equal(children.length, 2);
  assert.throws(() => process.kill(children[1].pid, 0), { code: 'ESRCH' });
});

test('mutation scope never reuses helper when the logical vault is replaced', async t => {
  const dir = await native.mkdtemp(path.join(tmpdir(), 'minni-claim-replaced-'));
  const vault = path.join(dir, 'vault'), moved = path.join(dir, 'original');
  await native.mkdir(vault);
  t.after(() => native.rm(dir, { recursive: true, force: true }));
  await adapter.withClaimFsScope(async () => {
    const root = await adapter.open(vault, DIR);
    const first = await adapter.startClaimFs(root);
    try {
      await native.rename(vault, moved); await native.mkdir(vault);
      const replacement = await adapter.open(vault, DIR);
      try {
        const second = await adapter.startClaimFs(replacement);
        assert.notEqual(second, first);
        await adapter.mkdir(`${first}/${adapter.claimHandleId(root)}/.runtime`, { mode: 0o700 });
        await assert.rejects(native.stat(path.join(vault, '.runtime')), { code: 'ENOENT' });
        assert.ok((await native.stat(path.join(moved, '.runtime'))).isDirectory());
      } finally { await adapter.closeClaimFs(replacement); await replacement.close(); }
    } finally { await adapter.closeClaimFs(root); await root.close(); }
  });
});
