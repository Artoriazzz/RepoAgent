"""修复测试网络并限制失败分析输出，续跑三道受外部故障影响的实验。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading


ROOT = Path('/home/artoria/workspace/SWE-bench')
PREVIOUS = ROOT / 'agent_work/runs/infra10-network-retry-glm-20261023'
OUTPUT = ROOT / 'agent_work/runs/external3-continuation-glm-20261023'
INSTANCES = ['psf__requests-2148', 'psf__requests-2317', 'pytest-dev__pytest-5221']


def launch():
    previous_config = json.loads((PREVIOUS / 'experiment.json').read_text())
    original = Path(previous_config['source_root'])
    for name, digest in previous_config['source_sha256'].items():
        if hashlib.sha256((original / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Frozen source changed: {name}')
    OUTPUT.mkdir(exist_ok=False)
    source = OUTPUT / 'source'
    shutil.copytree(original, source, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(Path(__file__).with_name('infra3-frozen-cli.py'), source / 'repo_agent/cli.py')
    config = {**previous_config, 'previous_run': str(PREVIOUS), 'source_root': str(source),
              'scheduled_ids': INSTANCES, 'review_max_output_tokens': 12288,
              'llm_timeout_seconds': 600, 'harness_timeout_seconds': 1800,
              'resume_interrupted_correction': True,
              'requests_test_proxy': 'http://127.0.0.1:7897'}
    config['source_sha256'] = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                               for name in previous_config['source_sha256']}
    (OUTPUT / 'experiment.json').write_text(json.dumps(config, indent=2))
    (OUTPUT / 'instances.json').write_text(json.dumps(INSTANCES, indent=2))
    with (OUTPUT / 'nohup.out').open('w') as log:
        process = subprocess.Popen([str(Path('/home/artoria/workspace/RepoAgent/RepoAgent/.venv/bin/python')),
                                    '-u', str(Path(__file__).resolve()), '--worker'],
                                   stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   start_new_session=True, cwd=ROOT)
    (OUTPUT / 'batch.pid').write_text(str(process.pid) + '\n')
    print(json.dumps({'pid': process.pid, 'output': str(OUTPUT), 'instances': INSTANCES}, indent=2))


def work():
    config = json.loads((OUTPUT / 'experiment.json').read_text())
    source = Path(config['source_root'])
    sys.path[:0] = [str(source), str(source / 'scripts'), str(PREVIOUS / 'runner')]
    import swebench_batch as batch

    live = Path('/home/artoria/workspace/RepoAgent/RepoAgent')
    originals = json.loads((PREVIOUS / 'results.json').read_text())
    by_id = {row['instance_id']: row for row in originals}
    dataset_rows = batch.load_instances(Path(config['dataset']), INSTANCES, 300, 0)
    rows = {row['instance_id']: row for row in dataset_rows}
    bypass = 'localhost,127.0.0.1,::1,open.bigmodel.cn,raw.githubusercontent.com,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn'
    environment = {**os.environ, **batch.read_env_file(live / '.env'), 'PYTHONPATH': str(source),
                   'NO_PROXY': bypass, 'no_proxy': bypass,
                   'REPO_AGENT_LLM_TIMEOUT_SECONDS': '600', 'REPO_AGENT_LLM_MAX_RETRIES': '2',
                   'REPO_AGENT_REVIEW_MAX_OUTPUT_TOKENS': '12288',
                   'SWEBENCH_REPO_MIRROR_ROOT': str(ROOT / 'agent_work/repo_cache'),
                   'SWEBENCH_RAW_URL_PREFIX': '', 'SWEBENCH_DOCKER_NETWORK': 'host',
                   'SWEBENCH_DOCKER_BUILD_NETWORK': 'host', 'SWEBENCH_PASS_PROXY_TO_DOCKER': '1'}
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        environment[key] = 'http://127.0.0.1:7897'
    # 子进程按同一冻结源码导入。
    os.environ['PYTHONPATH'] = str(source)
    lock = threading.Lock()
    results = []
    stages = {}

    def save(name, value):
        target = OUTPUT / name
        temp = target.with_suffix(target.suffix + '.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temp.replace(target)

    def progress(instance, stage):
        with lock:
            stages[instance] = {'stage': stage, 'timestamp': datetime.now(timezone.utc).isoformat()}
            save('progress.json', {'cases': stages, 'completed': len(results), 'total': 3})
            print(instance, stage, flush=True)

    def run_case(instance):
        row = rows[instance]
        old = by_id[instance]
        case = OUTPUT / 'cases' / instance
        case.mkdir(parents=True)
        env = {**environment, 'REPO_AGENT_LLM_LOG_DIR': str(case / 'llm-requests')}
        if instance.startswith('psf__requests'):
            env.update(SWEBENCH_TEST_PROXY='http://127.0.0.1:7897',
                       SWEBENCH_TEST_NO_PROXY='localhost,127.0.0.1,::1,10.255.255.1')
        args = batch.build_parser().parse_args([
            '--dataset', config['dataset'], '--instance-id', instance,
            '--repo-agent-bin', str(live / '.venv/bin/repo-agent'), '--model', config['model'],
            '--implement-model', config['model'], '--reasoning-effort', 'low',
            '--coordinator-reasoning-effort', 'high', '--implement-reasoning-effort', 'low',
            '--max-steps', '16', '--timeout', '3600', '--work-dir', str(ROOT / 'agent_work'),
            '--output-dir', str(case), '--offline-cache', '--repair-mode', 'experience-build',
            '--evaluate', '--swebench-dir', str(ROOT), '--harness-python', str(ROOT / '.venv/bin/python'),
            '--run-id', OUTPUT.name, '--harness-timeout', '1800', '--cache-level', 'instance',
            '--namespace', 'none', '--docker-network', 'host', '--candidate-mode', 'off',
            '--no-prebuild-instance-image', '--no-repair-memory',
            '--memory-root', str(OUTPUT / 'memory' / instance), '--memory-scope', row['repo'],
            '--memory-provenance', 'swebench-test-diagnostic'])
        if instance.startswith('psf__requests'):
            # 只复用初始产物；归因、纠正与验证仍沿用冻结的批处理逻辑。
            progress(instance, 'evaluating_saved_initial_patch')
            # batch 模块的补丁替换是全局的，两道请求题各用独立进程执行。
            previous_file = case / 'previous-result.json'
            previous_file.write_text(json.dumps(old, indent=2))
            command = [str(live / '.venv/bin/python'), '-u',
                       str(PREVIOUS / 'runner/reuse_lite_initial_generation.py'),
                       '--source-root', str(source), '--previous-result', str(previous_file), '--']
            # parser 保留的选项直接复用；每个进程独立导入 batch。
            command += ['--dataset', str(args.dataset), '--instance-id', instance,
                        '--repo-agent-bin', str(args.repo_agent_bin), '--model', args.model,
                        '--implement-model', args.implement_model, '--reasoning-effort', 'low',
                        '--coordinator-reasoning-effort', 'high', '--implement-reasoning-effort', 'low',
                        '--max-steps', '16', '--timeout', '3600', '--work-dir', str(args.work_dir),
                        '--output-dir', str(case), '--offline-cache', '--repair-mode', 'experience-build',
                        '--evaluate', '--swebench-dir', str(ROOT), '--harness-python', str(args.harness_python),
                        '--run-id', OUTPUT.name, '--harness-timeout', '1800', '--cache-level', 'instance',
                        '--namespace', 'none', '--docker-network', 'host', '--candidate-mode', 'off',
                        '--no-prebuild-instance-image', '--no-repair-memory',
                        '--memory-root', str(args.memory_root), '--memory-scope', row['repo'],
                        '--memory-provenance', 'swebench-test-diagnostic']
            with (case / 'run.log').open('w') as log:
                proc = subprocess.run(command, env=env, cwd=source, stdout=log, stderr=subprocess.STDOUT)
            path = case / 'batch_summary.json'
            result = json.loads(path.read_text())['instances'][0] if path.exists() else {
                'instance_id': instance, 'resolved': False, 'status': 'runner_failed'}
            result['process_exit_code'] = proc.returncode
        else:
            progress(instance, 'resuming_second_failure_review')
            previous_pass = old['passes'][0]
            interrupted = Path(previous_pass['output_dir']) / 'experience-correction-2'
            failed_patch = (interrupted / 'failed.patch').read_text()
            task = (interrupted / 'task.md').read_text()
            feedback = json.loads((interrupted / 'failed_validation.json').read_text())
            report = json.loads((interrupted / 'implementation_report.json').read_text())
            repo = case / 'repo'
            with (case / 'run.log').open('w') as log:
                for command in (["git", "clone", "--no-hardlinks", str(ROOT / 'agent_work/repos' / instance), str(repo)],
                                ["git", "-C", str(repo), "checkout", "--detach", row['base_commit']]):
                    subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
            batch.restore_worktree_patch(repo, failed_patch)
            correction = batch.run_experience_correction(row, repo, case / 'experience-correction-2',
                    task, failed_patch, feedback, report, args, env)
            candidate = correction.pop('model_patch', failed_patch)
            final_patch = failed_patch
            validation = batch.validate_model_patch(candidate, repo_dir=repo,
                        agent_output_dir=Path(correction['output_dir']), require_patch_review=False,
                        reject_failed_verification=False, stage_attribution=correction.get('stage_attribution'))
            if correction.get('agent_exit_code', 0) == 0 and not validation.get('discard_patch'):
                final_patch = candidate
            progress(instance, 'evaluating_resumed_correction')
            harness = batch.run_harness_for_instance(row, final_patch, case / 'final-evaluation', args, env, 2)
            (case / 'model.patch').write_text(final_patch)
            result = deepcopy(old)
            result.update(status='agent_ok', resolved=bool(harness.get('resolved')), api_errors=[],
                          output_dir=str(case), resumed_second_correction=True)
            result['passes'][0].update(harness=harness, experience_correction_2=correction)
            result['resumed_patch_validation'] = validation
            if correction.get('failure_review_exit_code', 0) != 0 or correction.get('agent_exit_code', 0) != 0:
                result['status'] = 'correction_error'
        with lock:
            results.append(result)
            save('new_results.json', results)
            overrides = {r['instance_id']: r for r in results}
            merged = [overrides.get(r['instance_id'], r) for r in originals]
            save('results.json', merged)
            first = lambda r: bool((r.get('passes') or [{}])[0].get('initial_harness',
                                     (r.get('passes') or [{}])[0].get('harness', {})).get('resolved'))
            save('summary.json', {'completed': len(results), 'total': 3,
                                 'new_resolved': sum(bool(r.get('resolved')) for r in results),
                                 'combined_total': len(merged),
                                 'initial_resolved': sum(first(r) for r in merged),
                                 'final_resolved': sum(bool(r.get('resolved')) for r in merged),
                                 'diagnostic_only': True})
        progress(instance, f'finished status={result["status"]} resolved={result.get("resolved")}')

    with ThreadPoolExecutor(max_workers=2) as pool:
        for result in pool.map(run_case, INSTANCES):
            pass
    print('All continuations finished.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    if parser.parse_args().worker:
        work()
    else:
        launch()
