"""Keep repeated cache timings distinct from first-pass retrieval evidence."""
from minni.eval.fixture import run_fixture


def test_single_pass_does_not_claim_repeated_measurements():
    report = run_fixture(repeats=1)
    assert report['summary']['repeated_pass_latency_s'] is None
    assert report['summary']['first_pass_latency_s'] == report['summary']['latency_s']
    assert report['options']['repeats'] == 1
    assert all(row['limit'] > 0 for row in report['queries'])


def test_repeated_report_groups_exact_observed_runs():
    report = run_fixture(repeats=2)
    for key, repeat in [('first_pass_latency_s', 0), ('repeated_pass_latency_s', 1)]:
        times = sorted(row['latency_s'] for row in report['queries'] if row['repetition'] == repeat)
        assert report['summary'][key]['p50'] == times[2]
        assert report['summary'][key]['max'] == times[-1]
    assert 'may hit caches' in report['timing_scope']
