"""冻结源码并单独验证测试设计，不启动修复角色或官方评测。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import traceback


SWE = Path('/home/artoria/workspace/SWE-bench')


def main():
    import pandas as pd

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--ids', nargs='+', required=True)
    args = parser.parse_args()
    run = args.run_dir
    run.mkdir(parents=True, exist_ok=False)
    source = run / 'source'
    shutil.copytree(args.source, source,
                    ignore=shutil.ignore_patterns('.git', '.venv', '__pycache__', '.env', '.env.*'))
    shutil.copyfile(__file__, run / 'orchestration.py')
    sys.path.insert(0, str(source / 'scripts'))
    from swebench_batch import read_env_file

    rows = pd.read_parquet(SWE / 'test-00000-of-00001.parquet').set_index('instance_id')
    selected = [{'id': instance, 'repo': rows.loc[instance, 'repo'],
                 'base_commit': rows.loc[instance, 'base_commit']} for instance in args.ids]
    env = {**os.environ, **read_env_file(Path('/home/artoria/workspace/RepoAgent/RepoAgent/.env'))}
    env.update(PYTHONPATH=str(source), PATH='/home/artoria/.local/bin:' + env.get('PATH', ''))
    for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
        env[key] = 'http://127.0.0.1:7897'
    (run / 'experiment.json').write_text(json.dumps({
        'instances': selected, 'source_origin': str(args.source), 'mode': 'design-only',
        'model': 'glm-5.3-flash', 'reasoning_effort': 'high', 'workers': 2,
        'repair_agents': False, 'official_feedback': False,
        'source_hashes': {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in source.rglob('*.py')},
    }, indent=2))
    states = {x['id']: {'status': 'queued'} for x in selected}
    lock = threading.Lock()

    def update(instance, **values):
        with lock:
            states[instance].update(values, updated_at=datetime.now(timezone.utc).isoformat())
            tmp = run / 'progress.tmp'
            tmp.write_text(json.dumps(states, indent=2))
            tmp.replace(run / 'progress.json')
            print(instance, values, flush=True)

    def execute(item):
        instance = item['id']
        case = run / 'cases' / instance
        case.mkdir(parents=True)
        try:
            update(instance, status='preparing')
            repo = case / 'repo'
            with (case / 'preparation.log').open('w') as log:
                subprocess.run(['git', 'clone', '--no-checkout',
                    str(SWE / 'agent_work/repo_cache' / item['repo'].replace('/', '__')), str(repo)],
                    check=True, stdout=log, stderr=subprocess.STDOUT)
                subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', item['base_commit']],
                               check=True, stdout=log, stderr=subprocess.STDOUT)
            task = case / 'task.md'
            task.write_text(rows.loc[instance, 'problem_statement'])
            image = f'sweb.eval.x86_64.{instance}:latest'
            image_id = subprocess.check_output(['docker', 'inspect', '--format', '{{.Id}}', image], text=True).strip()
            command = [sys.executable, '-m', 'repo_agent', 'design-public-tests', '--repo', str(repo),
                       '--task-file', str(task), '--model', 'glm-5.3-flash', '--reasoning-effort', 'high',
                       '--swebench-profile', '--no-repair-memory', '--repair-mode', 'benchmark',
                       '--output-dir', str(case / 'results' / instance / 'pass-1'),
                       '--memory-root', str(case / 'memory')]
            update(instance, status='designing', image=image, image_id=image_id)
            with (case / 'run.log').open('w') as log:
                result = subprocess.run(command, cwd=source, env={**env, 'REPO_AGENT_VERIFY_DOCKER_IMAGE': image},
                                        stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            update(instance, status='done' if result.returncode == 0 else 'error', exit_code=result.returncode)
        except Exception as error:
            (case / 'exception.log').write_text(traceback.format_exc())
            update(instance, status='error', error=f'{type(error).__name__}: {error}')

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(execute, selected))
    (run / 'finished.txt').write_text(datetime.now(timezone.utc).isoformat())
    return int(any(value['status'] != 'done' for value in states.values()))


if __name__ == '__main__':
    raise SystemExit(main())
