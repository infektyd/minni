"""Source-grounded labels and excerpts, independent of retrieval outcomes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from minni.eval.dataset import repo_root


def test_public_corpus_provenance_and_exact_source_excerpts():
    root = repo_root()
    data = json.loads((root / 'eval/fixtures/public_repo.json').read_text())
    assert data['provenance'] == 'machine-reviewed-public-repository'
    assert data['human_reviewed'] is False
    assert 'not representative' in data['scope']
    assert len(data['source_revision']) == 40
    blobs = {}
    for doc in data['documents']:
        source = doc['source']
        path = Path(source['path'])
        assert not path.is_absolute() and '..' not in path.parts
        assert path.parts[0] == 'docs'
        if str(path) not in blobs:
            blobs[str(path)] = subprocess.check_output(
                ['git', 'cat-file', 'blob', source['git_blob_oid']], cwd=root,
            )
        raw = blobs[str(path)]
        assert hashlib.sha256(raw).hexdigest() == source['sha256']
        lines = raw.decode().splitlines(keepends=True)
        assert 1 <= source['start_line'] <= source['end_line'] <= len(lines)
        assert doc['text'] == ''.join(lines[source['start_line'] - 1:source['end_line']])
        assert hashlib.sha256(doc['text'].encode()).hexdigest() == doc['text_sha256']
        assert doc['expected_eligible'] is True


def test_public_questions_have_independent_answers_and_resolvable_distractors():
    data = json.loads((repo_root() / 'eval/fixtures/public_repo.json').read_text())
    refs = {doc['ref'] for doc in data['documents']}
    queries = data['queries']
    assert 15 <= len(queries) <= 25
    assert len({query['case'] for query in queries}) == len(queries)
    assert len({query['query'] for query in queries}) == len(queries)
    assert {'specific-fact', 'paraphrase', 'multi-source', 'false-premise'} <= {q['category'] for q in queries}
    assert sum(q['category'] == 'multi-source' for q in queries) >= 3
    for query in queries:
        expected = set(query['expected_refs'])
        negatives = set(query['hard_negative_refs'])
        assert expected and expected <= refs
        assert negatives and negatives <= refs and not negatives & expected
        assert query['forbidden_refs'] == []  # topical distractors are authorized
        assert len(query['expected_answer'].split()) >= 7
        assert query['limit'] >= len(expected)
        if query['category'] == 'multi-source':
            assert len(expected) >= 2


def test_actual_loader_accepts_pinned_public_fixture():
    from minni.eval.fixture import load_fixture
    data = load_fixture(repo_root() / 'eval/fixtures/public_repo.json')
    assert len(data['queries']) == 20


import pytest


@pytest.mark.parametrize('tamper', [
    'provenance', 'human-review', 'revision', 'source-path', 'source-hash',
    'negative-range', 'past-end-range', 'excerpt', 'excerpt-and-hash',
    'text-hash', 'dangling-negative', 'missing-rationale', 'blob-oid', 'blob-type', 'missing-blob', 'wrong-source-path',
])
def test_actual_loader_rejects_tampered_public_provenance(tmp_path, tamper):
    from minni.eval.fixture import load_fixture
    data = json.loads((repo_root() / 'eval/fixtures/public_repo.json').read_text())
    doc = data['documents'][0]
    if tamper == 'provenance':
        data['provenance'] = 'human-reviewed'
    elif tamper == 'human-review':
        data['human_reviewed'] = True
    elif tamper == 'revision':
        data['source_revision'] = 'not-a-revision'
    elif tamper == 'blob-oid':
        doc['source']['git_blob_oid'] = 'HEAD:docs/concepts.md'
    elif tamper == 'blob-type':
        doc['source']['git_blob_oid'] = data['source_revision']
    elif tamper == 'missing-blob':
        doc['source']['git_blob_oid'] = '0' * 40
    elif tamper == 'wrong-source-path':
        doc['source']['path'] = 'docs/install.md'
    elif tamper == 'source-path':
        doc['source']['path'] = '../docs/concepts.md'
    elif tamper == 'source-hash':
        doc['source']['sha256'] = '0' * 64
    elif tamper == 'negative-range':
        doc['source']['start_line'] = -1
    elif tamper == 'past-end-range':
        doc['source']['end_line'] = 100000
    elif tamper in {'excerpt', 'excerpt-and-hash'}:
        doc['text'] += '\nInvented claim not present in the source.\n'
        if tamper == 'excerpt-and-hash':
            doc['text_sha256'] = hashlib.sha256(doc['text'].encode()).hexdigest()
    elif tamper == 'text-hash':
        doc['text_sha256'] = '0' * 64
    elif tamper == 'dangling-negative':
        data['queries'][0]['hard_negative_refs'] = ['public/missing.md']
    elif tamper == 'missing-rationale':
        data['queries'][0]['expected_answer'] = ''
    path = tmp_path / 'tampered.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_fixture(path)


def test_source_blobs_verify_after_squash_without_original_commit(tmp_path, monkeypatch):
    import minni.eval.fixture as fixture

    # Never let an invoking Git hook redirect disposable repository commands.
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    original_root = repo_root()
    data = json.loads((original_root / 'eval/fixtures/public_repo.json').read_text())
    # A brand-new repository contains only the public source tree, exactly as
    # a squash commit would. No branch/history from the original repo is used.
    for doc in data['documents']:
        source = doc['source']
        target = tmp_path / source['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(
            ['git', 'cat-file', 'blob', source['git_blob_oid']], cwd=original_root,
        ))
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', 'add', 'docs'], cwd=tmp_path, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Single source snapshot'],
                   cwd=tmp_path, check=True)
    assert subprocess.run(['git', 'cat-file', '-e', data['source_revision']],
                          cwd=tmp_path, stderr=subprocess.DEVNULL).returncode != 0
    path = tmp_path / 'corpus.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(fixture, 'repo_root', lambda: tmp_path)
    assert len(fixture.load_fixture(path)['documents']) == 18

    # Substituting a real but altered source blob must still fail the original
    # whole-file hash/excerpt contract; resolving an object alone is not enough.
    source = data['documents'][0]['source']
    changed = tmp_path / source['path']
    changed.write_text(changed.read_text() + '\nAltered source bytes.\n')
    altered_oid = subprocess.check_output(['git', 'hash-object', '-w', str(changed)],
                                          cwd=tmp_path, text=True).strip()
    subprocess.run(['git', 'add', source['path']], cwd=tmp_path, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Changed source'],
                   cwd=tmp_path, check=True)
    # Old snapshots remain verifiable after a later docs edit in the new history.
    assert len(fixture.load_fixture(path)['documents']) == 18
    source['git_blob_oid'] = altered_oid
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='hash or excerpt mismatch'):
        fixture.load_fixture(path)
