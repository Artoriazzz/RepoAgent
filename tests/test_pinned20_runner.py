import importlib.util
import json
from pathlib import Path


def runner(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('pinned20_runner', scripts / 'run_pinned20_wsl.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selection():
    return {'id': 'sample__repo-1', 'repo': 'sample/repo', 'saved_memories': {
        kind: {'filename': f'{kind}_example.md', 'arguments': {
            'name': 'example', 'type': kind, 'description': 'saved experience',
            'content': f' {kind} rule\n', 'provenance': 'verified historical repair'}}
        for kind in ('global_experience', 'repository_experience')}}


def test_seed_preserves_both_memories_and_audit_detects_mutation(tmp_path, monkeypatch):
    module = runner(monkeypatch)
    row = selection()
    manifest = module.seed_memories({'instances': [row]}, tmp_path)
    assert len(manifest['instances'][row['id']]) == 2
    root = tmp_path / 'memory' / row['id']
    hashes = module.memory_hashes(root)
    case = tmp_path / 'cases' / row['id']
    path = case / row['id'] / 'pass-1' / 'memory_injection.json'
    path.parent.mkdir(parents=True)
    audit = {'memories': [
        {'filename': Path(name).name, 'injected_content_sha256': sha}
        for name, sha in hashes.items() if Path(name).name != 'MEMORY.md']}
    path.write_text(json.dumps(audit))
    path.with_name('pinned_coordinator_input.md').write_text('\n'.join(
        saved['arguments']['content'] for saved in row['saved_memories'].values()))
    assert module.audit_case(case, row, hashes, root)['verified']
    correction = path.parent / 'experience-correction'
    correction.mkdir()
    assert not module.audit_case(case, row, hashes, root)['verified']
    correction.rmdir()
    # 与实际注入相同的完整文件哈希能通过；只计算正文哈希不能冒充成功审计。
    audit['memories'][0]['injected_content_sha256'] = module.hashlib.sha256(
        row['saved_memories']['global_experience']['arguments']['content'].strip().encode()).hexdigest()
    path.write_text(json.dumps(audit))
    assert not module.audit_case(case, row, hashes, root)['verified']
    audit['memories'] = [{'filename': Path(name).name, 'injected_content_sha256': sha}
                         for name, sha in hashes.items() if Path(name).name != 'MEMORY.md']
    path.write_text(json.dumps(audit))
    assert module.audit_case(case, row, hashes, root)['verified']
    next(root.rglob('*experience*.md')).write_text('changed')
    assert not module.audit_case(case, row, hashes, root)['verified']


def test_audit_rejects_missing_injection(tmp_path, monkeypatch):
    module = runner(monkeypatch)
    row = selection()
    case = tmp_path / 'case'
    assert module.audit_case(case, row, {}, tmp_path / 'empty')['reason'] == 'missing_injection_audit'


def test_retry_summary_uses_two_cases_without_changing_original_cohort(monkeypatch):
    module = runner(monkeypatch)
    results = [{'status': 'agent_ok', 'resolved': True, 'injection': {'verified': True}},
               {'status': 'api_error', 'resolved': False, 'injection': {'verified': True}}]
    summary = module.summarize(results, total=2)
    assert summary['total'] == 2 and summary['pass_at_1'] == 0.5
    assert summary['infrastructure_errors'] == 1
    assert module.summarize(results)['total'] == 20
