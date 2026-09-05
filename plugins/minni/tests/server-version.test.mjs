import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';

test('real MCP initialization reports the shipped package version', {timeout:15000}, async()=>{
  const root=fileURLToPath(new URL('../',import.meta.url));
  const pkg=JSON.parse(await readFile(path.join(root,'package.json'),'utf8'));
  const home=await mkdtemp(path.join(tmpdir(),'minni-version-'));
  const client=new Client({name:'version-test',version:'1'});
  try {
    await client.connect(new StdioClientTransport({command:process.execPath,
      args:[path.join(root,'dist/server.js')],cwd:home,stderr:'pipe',
      env:{PATH:process.env.PATH??'',HOME:home,MINNI_HOME:home,MINNI_AGENT_ID:'codex',
        MINNI_VAULT_PATH:path.join(home,'vault'),MINNI_SOCKET_PATH:path.join(home,'absent.sock')}}));
    assert.equal(client.getServerVersion().name,'minni');
    assert.equal(client.getServerVersion().version,pkg.version);
  } finally {
    await client.close();
    await rm(home,{recursive:true,force:true});
  }
});
