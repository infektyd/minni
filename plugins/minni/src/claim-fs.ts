/** Private descriptor-relative filesystem adapter for the claim store.
 * Linux uses real /proc/self/fd paths. Darwin's /dev/fd cannot traverse
 * children, so a stdlib Python child holds inherited fd3 and uses *at calls.
 * Synthetic paths are process-local capabilities, never filesystem fallbacks.
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { spawn, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import { constants, type Stats } from "node:fs";
import * as fs from "node:fs/promises";
import { CLAIM_FS_HELPER } from "./claim-fs-helper.js";

export interface FileHandle {
  readonly fd: number;
  stat(): Promise<Stats>;
  chmod(mode: number): Promise<void>;
  sync(): Promise<void>;
  close(): Promise<void>;
  readFile(encoding: "utf8"): Promise<string>;
  writeFile(text: string, encoding: "utf8"): Promise<void>;
}
const PREFIX = "/__minni_claim_fd__/";
const FRAME_MAX = 524288;
const TIMEOUT_MS = 10_000;
const sessions = new Map<string, Session>();
const roots = new WeakMap<FileHandle, Session>();
interface ClaimScope {
  closed: boolean;
  sessions: Map<string, Promise<Session>>;
}
const scopes = new AsyncLocalStorage<ClaimScope>();

/** Reuse interpreters only within one awaited mutation, never across requests.
 * Each lookup still opens and stats the real vault; dev+ino identifies the
 * inherited descriptor authority. Child descriptors close per location.
 */
export async function withClaimFsScope<T>(fn: () => Promise<T>): Promise<T> {
  if (scopes.getStore() && !scopes.getStore()!.closed) return fn();
  const scope: ClaimScope = { closed: false, sessions: new Map() };
  return scopes.run(scope, async () => {
    try { return await fn(); }
    finally {
      scope.closed = true;
      await Promise.all([...scope.sessions.values()].map(async pending => {
        try { await (await pending).stop(); } catch { /* startup already reaped */ }
      }));
    }
  });
}

export function claimHandleId(handle: FileHandle): number {
  return roots.get(handle)?.rootKey ?? handle.fd;
}
function error(code: string): NodeJS.ErrnoException {
  return Object.assign(new Error(`claim filesystem helper: ${code}`), { code });
}
function stats(raw: Record<string, number>): Stats {
  return { ...raw, isDirectory: () => (raw.mode & constants.S_IFMT) === constants.S_IFDIR,
    isFile: () => (raw.mode & constants.S_IFMT) === constants.S_IFREG } as Stats;
}
class Session {
  readonly alias = `${PREFIX}${randomUUID()}`;
  readonly child: ChildProcess;
  private next = 1;
  private pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: NodeJS.Timeout }>();
  private failure: Error | undefined;
  private buffer = "";
  private exited: Promise<void>;
  readonly rootKey: number;
  constructor(root: FileHandle, readonly scope?: ClaimScope) {
    this.rootKey = root.fd;
    // -I ignores PYTHONPATH, user site, and PYTHON* injection into the helper.
    const python = process.env.MINNI_CLAIM_PYTHON ?? process.env.PYTHON ?? process.env.PYTHON3 ?? "python3";
    this.child = spawn(python, ["-I", "-u", "-c", CLAIM_FS_HELPER, String(root.fd)],
      { stdio: ["pipe", "pipe", "ignore", root.fd] });
    this.exited = new Promise((resolve) => this.child.once("close", () => resolve()));
    this.child.on("error", () => this.fail(error("EHELPER")));
    this.child.on("exit", () => this.fail(error("EPIPE")));
    this.child.stdin!.on("error", () => this.fail(error("EPIPE")));
    this.child.stdout!.setEncoding("utf8");
    this.child.stdout!.on("data", (chunk: string) => {
      this.buffer += chunk;
      if (Buffer.byteLength(this.buffer) > FRAME_MAX) return this.fail(error("EFBIG"));
      let index: number;
      while ((index = this.buffer.indexOf("\n")) >= 0) {
        const line = this.buffer.slice(0, index); this.buffer = this.buffer.slice(index + 1);
        try {
          const msg = JSON.parse(line);
          const waiter = this.pending.get(msg.id);
          if (!waiter) return this.fail(error("EPROTO"));
          this.pending.delete(msg.id); clearTimeout(waiter.timer);
          if (msg.error) waiter.reject(error(String(msg.error)));
          else waiter.resolve(msg.result);
        } catch { this.fail(error("EPROTO")); }
      }
    });
    sessions.set(this.alias, this);
  }
  private fail(reason: Error): void {
    this.failure ??= reason;
    for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(this.failure); }
    this.pending.clear();
    if (this.child.exitCode === null && this.child.signalCode === null) this.child.kill("SIGKILL");
  }
  async request(op: string, args: Record<string, unknown>): Promise<unknown> {
    if (this.failure) throw this.failure;
    const id = this.next++;
    const line = `${JSON.stringify({ id, op, ...args })}\n`;
    if (Buffer.byteLength(line) > FRAME_MAX) throw error("EFBIG");
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail(error("ETIMEDOUT")), TIMEOUT_MS);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin!.write(line);
    });
  }
  async stop(): Promise<void> {
    sessions.delete(this.alias);
    this.fail(error("ECANCELED"));
    await this.exited;
  }
}
export async function startClaimFs(root: FileHandle): Promise<string> {
  const expected = await root.stat();
  const scope = scopes.getStore();
  const active = scope && !scope.closed ? scope : undefined;
  const identity = `${expected.dev}:${expected.ino}`;
  const create = async (): Promise<Session> => {
    const s = new Session(root, active);
    try {
      const actual = await s.request("hello", {}) as Record<string, number>;
      if (actual.dev !== expected.dev || actual.ino !== expected.ino) throw error("ESTALE");
      return s;
    } catch (e) { await s.stop(); throw e; }
  };
  let pending = active?.sessions.get(identity);
  if (!pending) {
    pending = create();
    active?.sessions.set(identity, pending);
  }
  const s = await pending;
  roots.set(root, s);
  return s.alias;
}
export async function closeClaimFs(root: FileHandle): Promise<void> {
  const s = roots.get(root);
  if (s) {
    roots.delete(root);
    if (!s.scope || s.scope.closed) await s.stop();
  }
}
function anchored(p: string): { session: Session; key: number; name?: string } | undefined {
  if (!p.startsWith(PREFIX)) return undefined;
  const pieces = p.slice(PREFIX.length).split("/");
  if (pieces.length < 2 || pieces.length > 3 || !/^\d+$/.test(pieces[1])) throw error("EINVAL");
  const session = sessions.get(`${PREFIX}${pieces[0]}`);
  if (!session) throw error("EBADF");
  return { session, key: Number(pieces[1]), name: pieces[2] };
}
class RemoteHandle implements FileHandle {
  constructor(readonly fd: number, private s: Session) {}
  async stat(): Promise<Stats> { return stats(await this.s.request("stat", { key: this.fd }) as Record<string, number>); }
  async chmod(mode: number): Promise<void> { await this.s.request("chmod", { key: this.fd, mode }); }
  async sync(): Promise<void> { await this.s.request("sync", { key: this.fd }); }
  async close(): Promise<void> { await this.s.request("close", { key: this.fd }); }
  async readFile(_encoding: "utf8"): Promise<string> { return await this.s.request("read", { key: this.fd }) as string; }
  async writeFile(text: string, _encoding: "utf8"): Promise<void> { await this.s.request("write", { key: this.fd, text }); }
}
export async function open(p: string, flags: number, mode?: number): Promise<FileHandle> {
  const a = anchored(p);
  if (!a) return fs.open(p, flags, mode);
  const kind = flags === (constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW) ? "directory"
    : flags === (constants.O_RDONLY | constants.O_NOFOLLOW) ? "read"
      : flags === (constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW) && mode === 0o600 ? "write" : undefined;
  if (!kind) throw error("EINVAL");
  const key = await a.session.request("open", { key: a.key, name: a.name, kind }) as number;
  return new RemoteHandle(key, a.session);
}
export async function mkdir(p: string, options: { mode: number }): Promise<void> {
  const a = anchored(p);
  if (!a) { await fs.mkdir(p, options); return; }
  if (options.mode !== 0o700) throw error("EINVAL");
  await a.session.request("mkdir", { key: a.key, name: a.name });
}
export async function lstat(p: string): Promise<Stats> {
  const a = anchored(p);
  return a ? stats(await a.session.request("lstat", { key: a.key, name: a.name }) as Record<string, number>) : fs.lstat(p);
}
export const stat = fs.stat;
export async function rename(from: string, to: string): Promise<void> {
  const a = anchored(from), b = anchored(to);
  if (!a && !b) return fs.rename(from, to);
  if (!a || !b || a.session !== b.session || a.key !== b.key) throw error("EXDEV");
  await a.session.request("rename", { key: a.key, name: a.name, target: b.name });
}
export async function unlink(p: string): Promise<void> {
  const a = anchored(p);
  if (!a) return fs.unlink(p);
  await a.session.request("unlink", { key: a.key, name: a.name });
}
export async function rmdir(p: string): Promise<void> {
  const a = anchored(p);
  if (!a) return fs.rmdir(p);
  await a.session.request("rmdir", { key: a.key, name: a.name });
}
export async function readdir(p: string, options: { withFileTypes: true }): Promise<Array<{ name: string; isDirectory(): boolean }>> {
  const a = anchored(p);
  if (!a) return fs.readdir(p, options);
  // Open the directory through its parent; the helper enumerates its pinned FD.
  const h = await open(p, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try {
    const entries = await a.session.request("list", { key: h.fd }) as Array<{ name: string; directory: boolean }>;
    return entries.map(e => ({ name: e.name, isDirectory: () => e.directory }));
  } finally { await h.close(); }
}

// Adapter seam used by claim operations and their deterministic race tests.
export const claimFs = { open, mkdir, lstat, stat, rename, unlink, rmdir, readdir };
