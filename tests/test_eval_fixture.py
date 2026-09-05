"""Actual retrieval fixture integration, including failed expectations and isolation."""
import json
from pathlib import Path

import pytest

from minni.eval.dataset import repo_root
from minni.eval.fixture import load_fixture, run_fixture


@pytest.fixture
def corpus_path(tmp_path):
    data = json.loads((repo_root() / 'eval/fixtures/retrieval.json').read_text())
    path = tmp_path / 'fixture.json'
    path.write_text(json.dumps(data))
    return path


def test_real_lexical_retrieval_has_ids_scope_and_honest_degradation(monkeypatch):
    from minni.retrieval import RetrievalEngine
    from minni.db import SovereignDB

    def no_model(_self):
        pytest.fail('lexical profile must not load a model')

    monkeypatch.setattr(RetrievalEngine, 'model', property(no_model))
    original = SovereignDB.__init__
    opened = []

    def track_database(self, config):
        opened.append(Path(config.db_path))
        original(self, config)

    monkeypatch.setattr(SovereignDB, '__init__', track_database)
    report = run_fixture(repeats=2)
    assert report['summary']['ok']
    assert report['human_reviewed'] is False
    assert report['summary']['runs'] == report['summary']['degraded_runs'] == 10
    assert len(opened) == 1 and not opened[0].exists()
    assert 'minni-eval-' in str(opened[0])
    rows = report['queries']
    for row in rows:
        assert row['latency_s'] >= 0
        assert 'deadline' in row['degradation']['vector']
        assert not row['forbidden_hits']
        assert all(report['document_ids'][ref] == doc_id for ref, doc_id in zip(row['result_refs'], row['result_doc_ids']))
    cross_project = next(row for row in rows if row['case'] == 'cross-project-recall')
    assert set(cross_project['result_refs']) == {'project-alpha/launch.md', 'project-beta/launch.md'}
    denied = next(row for row in rows if row['case'] == 'denied-only')
    assert denied['result_doc_ids'] == [] and denied['recall_at_limit'] is None


def test_expectations_are_not_used_to_manufacture_retrieval_results(corpus_path):
    data = json.loads(corpus_path.read_text())
    data['queries'][0]['expected_refs'] = ['own/private.md']
    data['queries'][0]['forbidden_refs'] = ['shared/evidence.md']
    corpus_path.write_text(json.dumps(data))
    report = run_fixture(corpus_path, repeats=1)
    assert report['summary']['ok'] is False
    row = report['queries'][0]
    assert row['missing_refs'] == ['own/private.md']
    assert row['forbidden_hits'] == ['shared/evidence.md']
    assert row['recall_at_limit'] == 0


@pytest.mark.parametrize('ref', ['../outside.md', '/tmp/outside.md', 'missing.md'])
def test_fixture_references_cannot_escape_or_dangle(corpus_path, ref):
    data = json.loads(corpus_path.read_text())
    if ref == 'missing.md':
        data['queries'][0]['expected_refs'] = [ref]
    else:
        data['documents'][0]['ref'] = ref
    corpus_path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_fixture(corpus_path)


def test_hybrid_refuses_silent_unembedded_index(monkeypatch):
    from minni.retrieval import RetrievalEngine
    monkeypatch.setattr(RetrievalEngine, 'index_durable_document', lambda *a, **kw: {'status': 'ok', 'chunks': 0})
    with pytest.raises(RuntimeError, match='no semantic chunks'):
        run_fixture(profile='hybrid', repeats=1)


def test_fixture_cli_writes_report_and_exits_nonzero_for_failed_checks(tmp_path, monkeypatch):
    from minni.eval.harness import main
    import minni.eval.fixture as fixture
    output = tmp_path / 'report.json'
    report = run_fixture(repeats=1)
    report['summary']['ok'] = False
    monkeypatch.setattr(fixture, 'run_fixture', lambda **kwargs: report)
    with pytest.raises(SystemExit) as exc:
        main(['fixture', '--output', str(output)])
    assert exc.value.code == 3
    assert json.loads(output.read_text())['summary']['ok'] is False


@pytest.mark.parametrize('injected_ref', ['foreign/private.md', 'blocked/code.md', 'own/draft.md', 'unknown'])
def test_oracle_rejects_unexpected_ineligible_or_unknown_results(monkeypatch, injected_ref):
    from minni.retrieval import RetrievalEngine
    original = RetrievalEngine.retrieve

    def inject(self, *args, **kwargs):
        results = original(self, *args, **kwargs)
        if injected_ref == 'unknown':
            doc_id = 999999
        else:
            with self.db.cursor() as cursor:
                doc_id = cursor.execute('SELECT doc_id FROM documents WHERE path LIKE ?',
                                        ('%/' + injected_ref,)).fetchone()[0]
        return results + [{'doc_id': doc_id}]

    monkeypatch.setattr(RetrievalEngine, 'retrieve', inject)
    report = run_fixture(repeats=1)
    row = report['queries'][0]  # shared-evidence has no case-local forbidden refs
    assert report['summary']['ok'] is False and row['ok'] is False
    if injected_ref == 'unknown':
        assert row['unknown_doc_ids'] == [999999]
    else:
        assert injected_ref in row['forbidden_hits']


def test_oracle_permits_unrelated_eligible_result(monkeypatch):
    from minni.retrieval import RetrievalEngine
    original = RetrievalEngine.retrieve

    def inject(self, *args, **kwargs):
        results = original(self, *args, **kwargs)
        with self.db.cursor() as cursor:
            doc_id = cursor.execute("SELECT doc_id FROM documents WHERE path LIKE '%/project-beta/launch.md'").fetchone()[0]
        return results + [{'doc_id': doc_id}]

    monkeypatch.setattr(RetrievalEngine, 'retrieve', inject)
    row = run_fixture(repeats=1)['queries'][0]
    assert row['ok'] and row['forbidden_hits'] == []


def test_every_document_requires_independent_eligibility_annotation(corpus_path):
    data = json.loads(corpus_path.read_text())
    del data['documents'][-1]['expected_eligible']
    corpus_path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='expected_eligible'):
        load_fixture(corpus_path)
