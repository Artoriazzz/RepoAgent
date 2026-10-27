import importlib.util
import json
from pathlib import Path


def load_script(name, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(name, scripts / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ranking_uses_vectors_not_case_ids_and_filters_repository(monkeypatch):
    module = load_script('vector_memory_agent', monkeypatch)
    records = [
        {'memory_filename': name, 'memory_type': kind, 'repository': repo,
         'vector_key': name, 'source_case_ids': [source]}
        for name, kind, repo, source in [
            ('same', 'global_experience', 'a', 'current'),
            ('other', 'global_experience', 'b', 'different'),
            ('repo-a', 'repository_experience', 'a', 'different'),
            ('repo-b', 'repository_experience', 'b', 'current')]]
    vectors = {'same': [[0, 1]], 'other': [[1, 0]], 'repo-a': [[0.8, 0.6]], 'repo-b': [[1, 0]]}
    ranked = module.rank_records(records, [[1, 0]], vectors, 'a')
    assert ranked['global_experience'][0]['memory_filename'] == 'other'
    assert [r['memory_filename'] for r in ranked['repository_experience']] == ['repo-a']


def test_selects_first35_corrected_cases_in_dataset_order(monkeypatch):
    module = load_script('run_vector35_wsl', monkeypatch)
    rows = [{'initial_resolved': False, 'final_resolved': True, 'dataset_index': n}
            for n in reversed(range(71))]
    rows.append({'initial_resolved': True, 'final_resolved': True, 'dataset_index': -1})
    eligible, selected = module.select_cases(rows)
    assert len(eligible) == 71
    assert [r['dataset_index'] for r in selected] == list(range(35))


def test_only_successful_new_memory_operations_are_restored(monkeypatch):
    module = load_script('run_vector35_wsl', monkeypatch)
    args = {'name': 'test', 'description': 'test', 'content': 'original', 'type': 'global_experience'}
    calls = [{'id': key, 'function': {'name': 'save_memory', 'arguments': json.dumps(args)}}
             for key in ('ok', 'failed', 'no-reply')]
    messages = [{'role': 'assistant', 'tool_calls': calls},
                {'role': 'tool', 'tool_call_id': 'ok', 'content': json.dumps({'status': 'success', 'data': {'filename': 'saved.md'}})},
                {'role': 'tool', 'tool_call_id': 'failed', 'content': json.dumps({'status': 'error'})}]
    assert list(module.successful_saves(messages)) == [(args, 'saved.md', 'ok')]


def test_remaining36_are_disjoint_and_keep_dataset_order(monkeypatch):
    module = load_script('run_vector_remaining36_wsl', monkeypatch)
    history = [{'instance_id': f'case-{i}', 'dataset_index': i} for i in reversed(range(71))]
    first = {'instances': [{'id': f'case-{i}'} for i in range(35)]}
    remaining = module.remaining_cases(history, first)
    assert [r['dataset_index'] for r in remaining] == list(range(35, 71))
