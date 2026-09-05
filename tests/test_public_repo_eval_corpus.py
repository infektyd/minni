"""Source-grounded labels and excerpts, independent of retrieval outcomes."""
import hashlib
import json
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
                ['git', 'show', f"{data['source_revision']}:{path}"], cwd=root,
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
    'text-hash', 'dangling-negative', 'missing-rationale',
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
        data['source_revision'] = '0' * 40
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
