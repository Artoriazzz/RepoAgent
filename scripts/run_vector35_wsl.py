"""冻结历史新经验库，在线向量召回，两路测首轮修复；不纠正、不学习。"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from run_pinned20_wsl import LIVE, SWE, WORK, HELPERS, digest, memory_hashes, save_json


BASELINE = WORK / 'runs/lite-pinned20-pass1-glm-20261025'
DEFAULT_OUTPUT = WORK / 'runs/lite-vector35-pass1-glm-20261026'


def select_cases(rows):
    eligible = sorted([r for r in rows if not r['initial_resolved'] and r['final_resolved']],
                      key=lambda r: r['dataset_index'])
    if len(eligible) != 71:
        raise ValueError(f'Expected 71 corrected successes, got {len(eligible)}')
    return eligible, eligible[:35]


def successful_saves(messages):
    replies = {m.get('tool_call_id'): m for m in messages if m.get('role') == 'tool'}
    for message in messages:
        for call in message.get('tool_calls') or []:
            tool = call['function']['name']
            if tool not in ('save_memory', 'update_memory') or call['id'] not in replies:
                continue
            reply = json.loads(replies[call['id']]['content'])
            arguments = json.loads(call['function']['arguments'])
            if tool == 'update_memory':
                filename = arguments.get('filename', '')
                kind = next((kind for kind in ('global_experience', 'repository_experience')
                             if filename.startswith(kind + '_')), None)
                if not kind or not all(arguments.get(key) for key in ('name', 'description', 'content')):
                    continue
                arguments = {key: arguments[key] for key in ('name', 'description', 'content')}
                arguments.update(type=kind, provenance='swebench-test-diagnostic')
            if reply.get('status') == 'success' and arguments.get('type') in ('global_experience', 'repository_experience'):
                yield arguments, reply.get('data', {}).get('filename'), call['id']


def audit_vector(case, selected, frozen_hashes, memory_root):
    path = case / selected['id'] / 'pass-1/memory_retrieval.json'
    if not path.exists():
        return {'verified': False, 'reason': 'missing_vector_retrieval_audit'}
    audit = json.loads(path.read_text())
    actual = {m['filename']: m['injected_content_sha256'] for m in audit['memories']}
    expected_rows = [ranked[0] for ranked in audit['rankings'].values() if ranked]
    expected = {r['memory_filename']: next(sha for name, sha in frozen_hashes.items()
                                         if Path(name).name == r['memory_filename']) for r in expected_rows}
    text = path.with_name('vector_coordinator_input.md').read_text()
    complete = all(r['content'].strip() in text for r in expected_rows)
    unchanged = memory_hashes(memory_root) == frozen_hashes
    forbidden = [str(p) for p in case.rglob('*') if p.is_dir() and
                 (p.name.startswith('experience-correction') or p.name == 'experience-curator')]
    return {'verified': expected == actual and unchanged and complete and not forbidden and audit['query_status'] == 'success',
            'injected_memory_count': len(actual), 'memory_unchanged': unchanged, 'both_contents_complete': complete,
            'unexpected_feedback_or_curator': forbidden, 'audit_path': str(path),
            'same_case_hit': {kind: bool(rows and selected['id'] in rows[0]['source_case_ids'])
                              for kind, rows in audit['rankings'].items()}}


def prepare(output):
    from repo_agent.core.memory import MemoryStore
    from vector_memory_agent import load_query_module
    from repo_agent.core.llm import ChatLLM
    import pandas as pd

    output.mkdir(parents=True, exist_ok=False)
    config = json.loads((BASELINE / 'experiment.json').read_text())
    source = Path(config['source_root'])
    for name, expected in config['source_sha256'].items():
        if digest(source / name) != expected:
            raise ValueError(f'Frozen repair source changed: {name}')
    if digest(Path(config['dataset'])) != config['dataset_sha256']:
        raise ValueError('Dataset changed')
    report = WORK / 'reports/lite-full-summary-20261024/instances.json'
    eligible, selected = select_cases(json.loads(report.read_text()))
    save_json(output / 'historical_cases.json', eligible)
    save_json(output / 'selection.json', {'instances': [
        {'id': r['instance_id'], 'index': r['dataset_index'], 'repo': r['repo']} for r in selected]})
    runner = output / 'runner'
    runner.mkdir()
    for name in HELPERS + ['run_pinned20_wsl.py', 'run_vector35_wsl.py', 'vector_memory_agent.py', 'embed_retrieval_texts.py']:
        shutil.copy2(Path(__file__).with_name(name), runner / name)
    shutil.copy2(Path(__file__).with_name('memory_query.py'), runner / 'memory_query.py')
    (runner / 'vector_memory_agent.py').chmod(0o755)
    corpus = output / 'corpus'
    corpus.mkdir()
    rows = {r['instance_id']: r for r in pd.read_parquet(config['dataset']).to_dict('records')}
    inventory = {r['id']: r for r in json.loads((WORK / 'pinned20-memory-inventory-20261025.json').read_text())}
    records = {}
    for row in eligible:
        instance = row['instance_id']
        entry = inventory.get(instance, {})
        directory = entry.get('curator_dir')
        if not directory:
            continue
        for path in sorted(Path(directory).glob('subagents/*/messages.json')):
            for arguments, filename, call_id in successful_saves(json.loads(path.read_text())):
                scope = '' if arguments['type'] == 'global_experience' else row['repo']
                key = hashlib.sha256(json.dumps([scope, arguments], sort_keys=True).encode()).hexdigest()
                if key in records:
                    records[key]['source_case_ids'].append(instance)
                    continue
                store = MemoryStore.for_scope(row['repo'], root=corpus / 'memory')
                unique_args = {**arguments, 'name': arguments['name'] + '-' + key[:12]}
                new_filename = store.save_memory(**unique_args)
                records[key] = {'memory_filename': new_filename, 'original_filename': filename,
                                'memory_type': arguments['type'], 'repository': row['repo'],
                                'source_case_ids': [instance], 'arguments': unique_args,
                                'content': arguments['content'], 'source_messages': str(path),
                                'source_messages_sha256': digest(path), 'tool_call_id': call_id}
    if not records:
        raise ValueError('No successful saved new memories found')
    save_json(corpus / 'saved_memory_sources.json', list(records.values()))
    annotations = json.loads((WORK / 'memory-retrieval/pinned20-enriched-20261026/annotations.json').read_text())['instances']
    needed = sorted({r['source_case_ids'][0] for r in records.values()})
    query_type = load_query_module().MemoryQueryAgent

    def extract(instance):
        if instance in annotations:
            return instance, annotations[instance]
        query = query_type(ChatLLM(model=config['model'], api_key=os.getenv('OPENAI_API_KEY'),
                          base_url=os.getenv('OPENAI_BASE_URL'), enable_thinking=False, reasoning_effort='low',
                          max_output_tokens=4096, output_limit_recovery=False)).extract(
                              issue_body=rows[instance]['problem_statement'],
                              output_dir=corpus / 'historical-queries' / instance)
        if query.status != 'success':
            raise RuntimeError(f'Historical query extraction failed: {instance}: {query.status}')
        return instance, {'problem_description': query.problem_description, 'usage_scenario': query.usage_scenario}

    fields = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(extract, instance) for instance in needed]
        for future in as_completed(futures):
            instance, value = future.result()
            fields[instance] = value
            save_json(output / 'progress.json', {'phase': 'historical_retrieval_fields', 'completed': len(fields), 'total': len(needed)})
            print(f'history {len(fields)}/{len(needed)} {instance}', flush=True)
    embedding_inputs = {}
    for record in records.values():
        field = fields[record['source_case_ids'][0]]
        text = f"问题描述：{field['problem_description']}\n使用场景：{field['usage_scenario']}"
        key = hashlib.sha256(text.encode()).hexdigest()
        record.update(**field, vector_key=key)
        embedding_inputs[key] = {'id': key, 'text': text, 'is_query': False}
    save_json(corpus / 'records.json', list(records.values()))
    save_json(corpus / 'embedding_input.json', list(embedding_inputs.values()))
    embedding_config = json.loads((WORK / 'memory-retrieval/pinned20-vector-top1-20261026/experiment.json').read_text())
    embedding_python = '/home/artoria/miniconda3/envs/llm_env/bin/python'
    subprocess.run([embedding_python, str(runner / 'embed_retrieval_texts.py'), '--input', str(corpus / 'embedding_input.json'),
                    '--output', str(corpus / 'embeddings.json'), '--model-path', embedding_config['embedding_model_path']], check=True)
    hashes = {}
    for row in selected:
        root = output / 'memory' / row['instance_id']
        store = MemoryStore.for_scope(row['repo'], root=root)
        for record in records.values():
            if record['memory_type'] == 'global_experience' or record['repository'] == row['repo']:
                origin_store = MemoryStore.for_scope(record['repository'], root=corpus / 'memory')
                directory = store.global_memory_dir if record['memory_type'] == 'global_experience' else store.memory_dir
                origin_dir = origin_store.global_memory_dir if record['memory_type'] == 'global_experience' else origin_store.memory_dir
                directory.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin_dir / record['memory_filename'], directory / record['memory_filename'])
        store.update_index()
        hashes[row['instance_id']] = memory_hashes(root)
    config.update(source_root=str(source), target_ids=[r['instance_id'] for r in selected],
                  corpus=str(corpus), embedding_python=embedding_python,
                  embedding_model_path=embedding_config['embedding_model_path'], memory_sha256=hashes,
                  agent_entry='vector_memory_agent.py', repair_stage='online_vector_retrieval_and_single_repair',
                  experiment='vector_top1_both_types_first35_corrected_successes', retrieval=True,
                  feedback_correction=False, memory_write=False, repair_mode='benchmark',
                  diagnostic_only=True, leaderboard_eligible=False, parallel_cases=2,
                  cohort_report=str(report), cohort_report_sha256=digest(report),
                  corpus_record_count=len(records), corpus_source_cases=len(needed), limit_per_type=1,
                  missing_saved_memory_cases=[r['instance_id'] for r in eligible if r['instance_id'] not in needed],
                  runner_sha256={p.name: digest(p) for p in runner.iterdir() if p.is_file()},
                  corpus_sha256={p.name: digest(p) for p in corpus.glob('*.json')},
                  network_no_proxy='localhost,127.0.0.1,::1,raw.githubusercontent.com,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn')
    save_json(output / 'experiment.json', config)
    (output / 'work').mkdir()
    (output / 'work/repo_cache').symlink_to(WORK / 'repo_cache', target_is_directory=True)
    save_json(output / 'progress.json', {'phase': 'repair_starting', 'completed': 0, 'total': 35})
    print(f'Corpus ready: {len(records)} memories from {len(needed)} cases; starting 35 repairs', flush=True)
    subprocess.run([str(SWE / '.venv/bin/python'), '-u', str(runner / 'run_vector35_wsl.py'),
                    '--worker', '--output', str(output)], cwd=SWE, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    if args.worker:
        import run_pinned20_wsl as runner
        runner.audit_case = audit_vector
        # 注入完整性单独报告，不改写真实的 Harness 通过结果。
        original_summarize = runner.summarize
        def summarize_results(results, total=35):
            summary = original_summarize(results, total)
            summary['resolved'] = sum(bool(row.get('resolved')) for row in results)
            summary['pass_at_1'] = summary['resolved'] / total
            summary['same_case_top1_hits'] = {
                kind: sum(r.get('injection', {}).get('same_case_hit', {}).get(kind, False) for r in results)
                for kind in ('global_experience', 'repository_experience')}
            return summary
        runner.summarize = summarize_results
        os.environ['REPO_AGENT_VECTOR_CONFIG'] = str(args.output / 'experiment.json')
        runner.work(args.output)
    else:
        for line in (LIVE / '.env').read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip().strip('\"\'')
        os.environ.update(REPO_AGENT_LLM_TIMEOUT_SECONDS='600', REPO_AGENT_LLM_MAX_RETRIES='2')
        prepare(args.output)
