"""两路执行同题经验直接注入诊断；不检索、不纠正、不学习。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import sys
import threading


SWE = Path('/home/artoria/workspace/SWE-bench')
WORK = SWE / 'agent_work'
LIVE = Path('/home/artoria/workspace/RepoAgent/RepoAgent')
BASELINE = WORK / 'runs/lite-external2-stream-env-retry-v2-glm-20261024'
DEFAULT_OUTPUT = WORK / 'runs/lite-pinned20-pass1-glm-20261025'
HELPERS = ['pinned_memory_agent.py', 'run_failed5_preflight_wsl.py',
           'resume_lite_parallel_wsl.py', 'repair_swebench_test_environment.py']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def memory_hashes(root):
    return {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob('*.md'))}


def seed_memories(selection, output):
    from repo_agent.core.memory import MemoryStore
    from pinned_memory_agent import inject_pinned_experiences

    manifest = {'diagnostic_only': True, 'instances': {}}
    for row in selection['instances']:
        store = MemoryStore.for_scope(row['repo'], root=output / 'memory' / row['id'])
        filenames = []
        for kind in ('global_experience', 'repository_experience'):
            saved = row['saved_memories'][kind]
            filename = store.save_memory(**saved['arguments'])
            if filename != saved['filename']:
                raise ValueError(f'Saved filename differs: {row["id"]}: {filename}')
            filenames.append(filename)
        text, memories = inject_pinned_experiences(task='PUBLIC_TASK', memory_store=store, filenames=filenames)
        if len(memories) != 2 or not all(m.content in text for m in memories):
            raise ValueError(f'Experience missing or clipped: {row["id"]}')
        manifest['instances'][row['id']] = filenames
    return manifest


def audit_case(case, selected, frozen_hashes, memory_root):
    path = case / selected['id'] / 'pass-1' / 'memory_injection.json'
    if not path.exists():
        return {'verified': False, 'reason': 'missing_injection_audit'}
    audit = json.loads(path.read_text())
    filenames = {saved['filename'] for saved in selected['saved_memories'].values()}
    # read_memory 返回含 frontmatter 的完整文件，审计也必须比较完整文件的冻结哈希。
    expected = {Path(name).name: sha for name, sha in frozen_hashes.items() if Path(name).name in filenames}
    actual = {m['filename']: m['injected_content_sha256'] for m in audit['memories']}
    unchanged = memory_hashes(memory_root) == frozen_hashes
    forbidden = [str(p) for p in case.rglob('*') if p.is_dir() and
                 (p.name.startswith('experience-correction') or p.name == 'experience-curator')]
    input_path = path.with_name('pinned_coordinator_input.md')
    text = input_path.read_text() if input_path.exists() else ''
    complete = all(saved['arguments']['content'].strip() in text for saved in selected['saved_memories'].values())
    return {'verified': set(expected) == filenames and expected == actual and unchanged and complete and not forbidden,
            'injected_memory_count': len(actual), 'memory_unchanged': unchanged,
            'both_contents_complete': complete,
            'unexpected_feedback_or_curator': forbidden, 'audit_path': str(path)}


def summarize(results, total=20):
    resolved = sum(bool(r.get('resolved')) and r.get('injection', {}).get('verified', False) for r in results)
    errors = sum(r.get('status') in {'environment_error', 'runner_failed', 'api_error'} or
                 any((p.get('harness') or {}).get('error') for p in r.get('passes', [])) for r in results)
    return {'completed': len(results), 'total': total, 'resolved': resolved, 'pass_at_1': resolved / total,
            'infrastructure_errors': errors,
            'injection_verified': sum(r.get('injection', {}).get('verified', False) for r in results),
            'historical_initial_resolved': 0, 'diagnostic_only': True, 'leaderboard_eligible': False}


def reaudit(output):
    """只重算报告，不重跑 Agent/Harness；原始报告留存供追溯。"""
    config = json.loads((output / 'experiment.json').read_text())
    selected = {r['id']: r for r in json.loads((output / 'selection.json').read_text())['instances']}
    results = json.loads((output / 'results.json').read_text())
    for name in ('results.json', 'summary.json'):
        backup = output / (name + '.before-audit-fix')
        if not backup.exists():
            shutil.copyfile(output / name, backup)
    for result in results:
        instance = result['instance_id']
        result['injection'] = audit_case(output / 'cases' / instance, selected[instance],
                                        config['memory_sha256'][instance], output / 'memory' / instance)
    save_json(output / 'results.json', results)
    summary = summarize(results, len(config['target_ids']))
    summary['audit_fix_script_sha256'] = digest(Path(__file__))
    save_json(output / 'summary.json', summary)
    print(json.dumps(summary, indent=2))


def launch(output, selection_path):
    baseline = json.loads((BASELINE / 'experiment.json').read_text())
    original = Path(baseline['source_root'])
    for name, expected in baseline['source_sha256'].items():
        if digest(original / name) != expected:
            raise ValueError(f'Frozen source changed: {name}')
    selection = json.loads(selection_path.read_text())
    ids = [r['id'] for r in selection['instances']]
    if len(ids) != 20 or len(set(ids)) != 20:
        raise ValueError('Expected exactly 20 unique selected cases')
    output.mkdir(parents=True, exist_ok=False)
    source = output / 'source'
    shutil.copytree(original, source, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    runner = output / 'runner'
    runner.mkdir()
    for name in HELPERS + [Path(__file__).name]:
        shutil.copyfile(Path(__file__).with_name(name), runner / name)
    (runner / 'pinned_memory_agent.py').chmod(0o755)
    sys.path.insert(0, str(source))
    manifest = seed_memories(selection, output)
    save_json(output / 'selection.json', selection)
    save_json(output / 'memory-manifest.json', manifest)
    config = {
        'diagnostic_only': True, 'leaderboard_eligible': False,
        'experiment': 'same_case_pinned_global_and_repository_experience',
        'source_root': str(source), 'source_sha256': baseline['source_sha256'],
        'dataset': baseline['dataset'], 'dataset_sha256': baseline['dataset_sha256'],
        'target_ids': ids, 'parallel_cases': 2, 'repair_mode': 'benchmark',
        'retrieval': False, 'feedback_correction': False, 'memory_write': False,
        'model': 'glm-5.3-flash', 'max_steps': 16,
        'coordinator_reasoning_effort': 'high', 'other_reasoning_effort': 'low',
        'historical_initial_resolved': 0, 'historical_final_resolved': 20,
        'memory_sha256': {r['id']: memory_hashes(output / 'memory' / r['id']) for r in selection['instances']},
        'runner_sha256': {p.name: digest(p) for p in runner.iterdir()},
    }
    save_json(output / 'experiment.json', config)
    (output / 'work').mkdir()
    (output / 'work/repo_cache').symlink_to(WORK / 'repo_cache', target_is_directory=True)
    with (output / 'nohup.out').open('w') as log:
        process = subprocess.Popen([str(SWE / '.venv/bin/python'), '-u',
                                    str(runner / Path(__file__).name), '--worker', '--output-dir', str(output)],
                                   cwd=SWE, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    (output / 'batch.pid').write_text(str(process.pid) + '\n')
    print(json.dumps({'pid': process.pid, 'output': str(output), 'cases': 20, 'parallel': 2}))


def work(output):
    config = json.loads((output / 'experiment.json').read_text())
    source = Path(config['source_root'])
    sys.path[:0] = [str(Path(__file__).parent), str(source), str(source / 'scripts')]
    import docker
    import pandas as pd
    from run_failed5_preflight_wsl import clean_cached_test_image, prepare_image
    from resume_lite_parallel_wsl import REPO_IMPORTS, cached_baseline_check

    if digest(Path(config['dataset'])) != config['dataset_sha256']:
        raise ValueError('Dataset changed')
    for name, expected in config['source_sha256'].items():
        if digest(source / name) != expected:
            raise ValueError(f'Frozen source changed: {name}')
    selections = json.loads((output / 'selection.json').read_text())['instances']
    total = len(selections)
    by_id = {r['instance_id']: r for r in pd.read_parquet(config['dataset']).to_dict('records')}
    queue = Queue()
    for selected in selections:
        queue.put(selected)
    proxy = 'http://127.0.0.1:7897'
    bypass = config.get('network_no_proxy', 'localhost,127.0.0.1,::1,open.bigmodel.cn,raw.githubusercontent.com,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn')
    env = {**os.environ, 'PYTHONPATH': str(source), 'PATH': str(LIVE / '.venv/bin') + ':' + os.environ['PATH'],
           'NO_PROXY': bypass, 'no_proxy': bypass, 'REPO_AGENT_LLM_TIMEOUT_SECONDS': '600',
           'REPO_AGENT_LLM_MAX_RETRIES': '2', 'HF_HUB_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1',
           'REPO_AGENT_PINNED_MEMORY_MANIFEST': str(output / 'memory-manifest.json'),
           'SWEBENCH_REPO_MIRROR_ROOT': str(WORK / 'repo_cache'), 'SWEBENCH_RAW_URL_PREFIX': '',
           'SWEBENCH_PASS_PROXY_TO_DOCKER': '1', 'SWEBENCH_DOCKER_BUILD_NETWORK': 'host',
           'SWEBENCH_DOCKER_NETWORK': 'host', 'SWEBENCH_GIT_PROXY': proxy}
    env.update({key: proxy for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')})
    os.environ.update({k: v for k, v in env.items() if k.startswith('SWEBENCH_') or 'proxy' in k.lower()})
    lock = threading.Lock()
    results, lanes = [], {}

    def save_results():
        save_json(output / 'results.json', results)
        save_json(output / 'summary.json', summarize(results, total))
        if config.get('previous_run'):
            previous = json.loads((Path(config['previous_run']) / 'results.json').read_text())
            updates = {r['instance_id']: r for r in results}
            merged = [updates.get(r['instance_id'], r) for r in previous]
            save_json(output / 'combined_results.json', merged)
            save_json(output / 'combined_summary.json', summarize(merged, len(merged)))

    def progress(lane, instance, stage):
        with lock:
            lanes[lane] = {'instance_id': instance, 'stage': stage,
                           'timestamp': datetime.now(timezone.utc).isoformat()}
            save_json(output / 'progress.json', {'completed': len(results), 'total': total, 'lanes': lanes})
            print(f'{lanes[lane]["timestamp"]} {lane} {instance} {stage}', flush=True)

    def run_lane(lane):
        client = docker.from_env()
        while True:
            try:
                selected = queue.get_nowait()
            except Empty:
                return
            instance = selected['id']
            row = by_id[instance]
            case = output / 'cases' / instance
            case.mkdir(parents=True)
            progress(lane, instance, 'preflight')
            try:
                with (case / 'environment.log').open('w') as log:
                    image = f'sweb.eval.x86_64.{instance}:latest'
                    try:
                        cached = client.images.get(image)
                    except docker.errors.ImageNotFound:
                        cached = None
                    if cached:
                        check = cached_baseline_check(row, cached.labels) + REPO_IMPORTS[row['repo']]
                        data = client.containers.run(image, ['/opt/miniconda3/envs/testbed/bin/python', '-c', check], remove=True)
                        log.write(data.decode())
                        environment = {'status': 'ready', 'image_id': cached.id, 'cached': True}
                    else:
                        environment = prepare_image(row, SWE, client, log, imports=REPO_IMPORTS[row['repo']],
                            proxy=proxy, wheelhouse=WORK / 'build-wheelhouse' if row['repo'] == 'django/django' else None,
                            apt_mirror='https://mirrors.ustc.edu.cn/ubuntu', build_assets=WORK / 'build-assets',
                            canonical=config.get('canonical_environment_builds', False), clean_tests=True)
                    environment.update(clean_cached_test_image(row, SWE, client, log, proxy=proxy))
                save_json(case / 'environment.json', environment)
                progress(lane, instance, config.get('repair_stage', 'single_attempt_with_pinned_memory'))
                command = [str(LIVE / '.venv/bin/python'), str(source / 'scripts/swebench_batch.py'),
                    '--dataset', config['dataset'], '--repo-agent-bin', str(Path(__file__).with_name(config.get('agent_entry', 'pinned_memory_agent.py'))),
                    '--model', config['model'], '--implement-model', config['model'], '--reasoning-effort', 'low',
                    '--coordinator-reasoning-effort', 'high', '--implement-reasoning-effort', 'low', '--max-steps', '16',
                    '--work-dir', str(output / 'work'), '--output-dir', str(case), '--env-file', str(LIVE / '.env'),
                    '--proxy', proxy, '--offline-cache', '--instance-id', instance, '--repair-mode', 'benchmark',
                    '--timeout', '3600', '--evaluate', '--swebench-dir', str(SWE),
                    '--harness-python', str(SWE / '.venv/bin/python'), '--run-id', output.name,
                    '--harness-timeout', '900', '--cache-level', 'instance', '--namespace', 'none',
                    '--docker-network', 'host', '--candidate-mode', 'off', '--no-prebuild-instance-image',
                    '--memory-root', str(output / 'memory' / instance), '--memory-scope', row['repo'],
                    '--memory-provenance', 'swebench-test-pinned-diagnostic']
                with (case / 'run.log').open('w') as log:
                    proc = subprocess.run(command, cwd=source, env={**env, 'REPO_AGENT_LLM_LOG_DIR': str(case / 'llm-requests')},
                                          stdout=log, stderr=subprocess.STDOUT)
                path = case / 'batch_summary.json'
                result = json.loads(path.read_text())['instances'][0] if path.exists() else {
                    'instance_id': instance, 'status': 'runner_failed', 'resolved': False}
                result['process_exit_code'] = proc.returncode
                result['injection'] = audit_case(case, selected, config['memory_sha256'][instance], output / 'memory' / instance)
            except Exception as exc:
                result = {'instance_id': instance, 'status': 'environment_error', 'resolved': False, 'error': repr(exc)}
                save_json(case / 'error.json', result)
            result.update(diagnostic_only=True, leaderboard_eligible=False, output_dir=str(case))
            with lock:
                results.append(result)
                save_results()
            progress(lane, instance, f'finished status={result.get("status")} resolved={result.get("resolved")}')
        client.close()

    save_results()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_lane, lane) for lane in ('lane1', 'lane2')]
        for future in futures:
            future.result()
    print(f'All {total} pinned-memory diagnostics finished.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--summarize', action='store_true')
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--selection', type=Path, default=WORK / 'pinned20-selection-20261025.json')
    args = parser.parse_args()
    if args.summarize:
        reaudit(args.output_dir)
    elif args.worker:
        work(args.output_dir)
    else:
        launch(args.output_dir, args.selection)
