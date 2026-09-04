// Trusted, embedded stdlib-only helper: bundled server and wheel payload carry
// exactly the same code. No temporary executable or checkout path is needed.
export const CLAIM_FS_HELPER = String.raw`
import errno, json, os, re, stat, sys
MAX = 65536
FRAME = 524288
root_key = int(sys.argv[1])
handles = {root_key: (3, 'vault')}
next_key = root_key + 1
DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
READ = os.O_RDONLY | os.O_NOFOLLOW
WRITE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

def fail(code=errno.EINVAL):
    raise OSError(code, 'claim filesystem operation refused')

def entry(key):
    if type(key) is not int or key not in handles: fail(errno.EBADF)
    return handles[key]

def child(key, name, file_ok=True):
    fd, role = entry(key)
    if not isinstance(name, str) or not name or '/' in name or '\\' in name or name in ('.', '..'): fail()
    rules = {'vault': (r'\.runtime', 'runtime'), 'runtime': (r'thread-claims', 'claims'),
             'claims': (r'[0-9a-f]{32}', 'plan'), 'updates': (r'[0-9a-f]{32}', 'slice'),
             'slice': (r'g[0-9]+', 'generation')}
    if role in rules:
        pattern, dest = rules[role]
        if re.fullmatch(pattern, name): return fd, dest
    if role == 'plan' and name == 'updates': return fd, 'updates'
    if file_ok and role in ('plan', 'generation') and re.fullmatch(r'(?:[0-9a-f]{32}\.json|\.[0-9a-f]{32}(?:\.json)?\.[0-9a-f]{32}\.tmp)', name):
        return fd, 'file'
    fail()

def info(s):
    return {k: getattr(s, 'st_' + k) for k in ('dev','ino','mode','nlink','size')}

def private_file(fd):
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_mode & 0o777 != 0o600: fail(errno.EPERM)
    if s.st_size > MAX: fail(errno.EFBIG)
    return s

def run(q):
    global next_key
    op = q.get('op')
    if op == 'hello':
        s = os.fstat(3)
        if not stat.S_ISDIR(s.st_mode): fail(errno.ENOTDIR)
        return info(s)
    key = q.get('key')
    fd, role = entry(key)
    if op == 'stat': return info(os.fstat(fd))
    if op == 'close':
        if key == root_key: fail()
        os.close(fd); del handles[key]; return None
    if op == 'sync': os.fsync(fd); return None
    if op == 'chmod':
        mode = 0o600 if role == 'file' else 0o700
        if q.get('mode') != mode: fail()
        os.fchmod(fd, mode); return None
    if op == 'read':
        if role != 'file': fail()
        private_file(fd)
        data = bytearray()
        while len(data) <= MAX:
            part = os.read(fd, min(8192, MAX + 1 - len(data)))
            if not part: break
            data.extend(part)
        if len(data) > MAX: fail(errno.EFBIG)
        private_file(fd)
        return data.decode('utf-8')
    if op == 'write':
        if role != 'file' or not isinstance(q.get('text'), str): fail()
        data = q['text'].encode('utf-8')
        if len(data) > MAX: fail(errno.EFBIG)
        private_file(fd)
        while data:
            n = os.write(fd, data)
            if n <= 0: fail(errno.EIO)
            data = data[n:]
        return None
    if op == 'list':
        if role != 'generation': fail()
        names = os.listdir(fd)
        if len(names) > 4096: fail(errno.EFBIG)
        # Report shape, never follow directory-entry symlinks.
        return [{'name': n, 'directory': stat.S_ISDIR(os.stat(n, dir_fd=fd, follow_symlinks=False).st_mode)} for n in names]
    name = q.get('name')
    parent, dest = child(key, name)
    if op == 'mkdir':
        if dest == 'file': fail()
        os.mkdir(name, 0o700, dir_fd=parent); return None
    if op == 'open':
        kind = q.get('kind')
        if kind == 'directory' and dest != 'file': flags = DIR
        elif kind == 'read' and dest == 'file': flags = READ
        elif kind == 'write' and dest == 'file': flags = WRITE
        else: fail()
        opened = os.open(name, flags, 0o600, dir_fd=parent)
        if len(handles) >= 32:
            os.close(opened); fail(errno.EMFILE)
        ident = next_key; next_key += 1
        handles[ident] = (opened, dest)
        return ident
    if op == 'lstat': return info(os.stat(name, dir_fd=parent, follow_symlinks=False))
    if op == 'unlink':
        if dest != 'file': fail()
        os.unlink(name, dir_fd=parent); return None
    if op == 'rmdir':
        if role != 'slice' or dest != 'generation': fail()
        os.rmdir(name, dir_fd=parent); return None
    if op == 'rename':
        target = q.get('target')
        _, target_role = child(key, target)
        if dest != 'file' or target_role != 'file' or not name.startswith('.') or not re.fullmatch(r'[0-9a-f]{32}\.json', target): fail()
        os.rename(name, target, src_dir_fd=parent, dst_dir_fd=parent); return None
    fail()

try:
    while True:
        line = sys.stdin.buffer.readline(FRAME + 1)
        if not line: break
        if len(line) > FRAME or not line.endswith(b'\n'): break
        ident = None
        try:
            q = json.loads(line)
            if not isinstance(q, dict): fail()
            ident = q.get('id')
            if type(ident) is not int: fail()
            result = run(q)
            response = {'id': ident, 'result': result}
        except OSError as exc:
            response = {'id': ident, 'error': errno.errorcode.get(exc.errno, 'EIO')}
        except Exception:
            response = {'id': ident, 'error': 'EINVAL'}
        encoded = json.dumps(response, ensure_ascii=True).encode() + b'\n'
        if len(encoded) > FRAME: encoded = json.dumps({'id': ident, 'error': 'EFBIG'}).encode() + b'\n'
        sys.stdout.buffer.write(encoded); sys.stdout.buffer.flush()
finally:
    for fd, _ in handles.values():
        try: os.close(fd)
        except OSError: pass
`;
