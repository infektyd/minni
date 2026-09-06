"""Private snapshot search must retain least privilege and eligible recall."""
import pytest
from minni.eval.retrievers import SnapshotSearcher
from minni.eval.study_snapshot import prepare_snapshot, materialize_snapshot_db
from test_eval_study_snapshot import _packet, _record

@pytest.mark.parametrize('agent_id', ['main', 'operator'])
def test_reserved_identity_cannot_gain_governance(tmp_path, agent_id):
    dest = tmp_path / 'snapshot'
    prepare_snapshot(_packet([_record(agent='foreign', privacy_level='private')],
                             principal={'agent_id': agent_id, 'capabilities': []}), dest)
    materialize_snapshot_db(dest)
    with pytest.raises(ValueError, match='operator'):
        SnapshotSearcher(dest)

def test_eligible_result_after_more_than_scan_window(tmp_path):
    dest = tmp_path / 'snapshot'
    rows = [_record(str(i), path=f'private/{i}.md', text='alpha', agent='foreign')
            for i in range(110)]
    rows.append(_record('eligible', path='own/result.md', text='alpha with longer descriptive text'))
    prepare_snapshot(_packet(rows), dest)
    materialize_snapshot_db(dest)
    result = SnapshotSearcher(dest).search('alpha', limit=10)
    assert len(result) == 1
    assert result[0]['source'].endswith('own/result.md')
