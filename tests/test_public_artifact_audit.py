import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    'public_artifact_audit', Path(__file__).parents[1] / 'scripts/audit_public_tests_wsl.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def make_outputs(tmp_path, predicted='final corrected patch'):
    case = tmp_path / 'results/owner__repo-1'
    (case / 'pass-1').mkdir(parents=True)
    (case / 'pass-1/model.patch').write_text('initial incomplete patch')
    summary = case / 'summary.json'
    summary.write_text(json.dumps({'model_patch': 'final corrected patch'}))
    (case.parent / 'predictions.json').write_text(json.dumps([
        {'instance_id': case.name, 'model_patch': predicted}]))
    return summary


def test_final_patch_uses_submitted_correction_not_initial_artifact(tmp_path):
    path = make_outputs(tmp_path)
    assert audit.final_patch(path) == 'final corrected patch'


def test_final_patch_rejects_mismatched_submission(tmp_path):
    path = make_outputs(tmp_path, predicted='different submission')
    with pytest.raises(ValueError, match='summary/prediction patch mismatch'):
        audit.final_patch(path)


def test_audit_preserves_completed_results_and_counts_missing_separately(tmp_path, monkeypatch):
    base, retry, output = (tmp_path / name for name in ('base', 'retry', 'audit'))
    instances = {f'owner__repo-{i}': {'status': status} for i, status in enumerate(
        ('done', 'environment_error', 'environment_error', 'running', 'done', 'waiting_for_api_credit'), 1)}

    def write_summary(run, instance, status, patch, resolved):
        results = run / 'cases' / instance / 'results'
        (results / instance).mkdir(parents=True)
        (results / instance / 'summary.json').write_text(json.dumps({
            'status': status, 'model_patch': patch, 'resolved': resolved,
        }))
        (results / 'predictions.json').write_text(json.dumps([
            {'instance_id': instance, 'model_patch': patch}]))

    good, repaired, missing, running, api_error, waiting = instances
    write_summary(base, good, 'agent_ok', 'original patch', True)
    write_summary(retry, good, 'invalid_patch', '', False)
    write_summary(retry, repaired, 'invalid_patch', '', False)
    write_summary(retry, running, 'agent_ok', 'unrelated patch', True)
    write_summary(base, api_error, 'api_error', '', False)
    write_summary(retry, api_error, 'agent_ok', 'retry patch', True)
    write_summary(retry, waiting, 'agent_ok', 'first patch after quota renewal', True)
    (base / 'experiment.json').write_text(json.dumps({'instances': [
        {'id': instance, 'repo': 'owner/repo'} for instance in instances]}))
    (base / 'progress.json').write_text(json.dumps({'instances': instances}))
    monkeypatch.setattr('sys.argv', ['audit', '--base-run', str(base), '--retry-run',
                                    str(retry), '--output', str(output)])

    audit.main()

    result = json.loads((output / 'summary.json').read_text())
    assert result['resolved'] == 3
    assert result['empty_patches'] == 1
    assert result['missing_summaries'] == 2
    cases = {case['instance_id']: case for case in result['cases']}
    assert cases[good]['resolved'] is True
    assert cases[repaired]['patch_empty'] is True
    assert cases[missing]['status'] == 'environment_error'
    assert cases[missing]['patch_empty'] is None
    assert cases[running]['resolved'] is None
    assert cases[api_error]['resolved'] is True
    assert cases[waiting]['resolved'] is True
