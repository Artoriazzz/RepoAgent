"""修复预检后，两路补跑 79 题并重跑第 300 题，合并已有结果。"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path('/home/artoria/workspace/SWE-bench')
WORK = ROOT / 'agent_work'
PREVIOUS = WORK / 'runs/lite200to299-requests-network-glm-20261024'
OUTPUT = WORK / 'runs/lite200to299-env79-plus300-glm-20261024'


def main():
    import pandas as pd

    config = json.loads((PREVIOUS / 'experiment.json').read_text())
    source = Path(config['source_root'])
    for name, digest in config['source_sha256'].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Frozen source changed: {name}')
    old = json.loads((PREVIOUS / 'results.json').read_text())
    failed = [r['instance_id'] for r in old if r['status'] == 'environment_error']
    last = pd.read_parquet(config['dataset']).iloc[299]['instance_id']
    if len(failed) != 79 or last in failed:
        raise RuntimeError('Expected 79 preflight failures plus the separate 300th instance')
    checked = json.loads((WORK / 'environment-repair-check-20261024/cached-checks.json').read_text())
    if len(checked) != 69 or not all(r['ready'] for r in checked):
        raise RuntimeError('Cached SymPy environment checks are incomplete')
    OUTPUT.mkdir(exist_ok=False)
    runner = OUTPUT / 'runner'
    shutil.copytree(PREVIOUS / 'runner', runner, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('resume_lite_parallel_wsl.py', 'run_failed5_preflight_wsl.py'):
        shutil.copyfile(Path(__file__).with_name(name), runner / name)
    shutil.copytree(WORK / 'environment-repair-check-20261024', OUTPUT / 'preflight-checks')
    for name in ('cleanup-successful-20261024.json',):
        shutil.copyfile(WORK / name, OUTPUT / name)
    shutil.copyfile('/tmp/repoagent-route-probe.json', OUTPUT / 'network-route.json')
    (OUTPUT / 'rerun-instances.json').write_text(json.dumps(failed + [last], indent=2))
    bypass = 'localhost,127.0.0.1,::1,open.bigmodel.cn,raw.githubusercontent.com,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn'
    env = {**os.environ, 'REPO_AGENT_NETWORK_NO_PROXY': bypass, 'NO_PROXY': bypass, 'no_proxy': bypass,
           'REPO_AGENT_LLM_TIMEOUT_SECONDS': '600', 'REPO_AGENT_LLM_MAX_RETRIES': '2',
           'REPO_AGENT_REVIEW_MAX_OUTPUT_TOKENS': '12288'}
    command = [str(ROOT / '.venv/bin/python'), '-u', str(runner / 'resume_lite_parallel_wsl.py'),
               '--previous-run', str(PREVIOUS), '--source-root', str(source), '--output-dir', str(OUTPUT),
               '--wheelhouse', str(WORK / 'build-wheelhouse'), '--build-assets', str(WORK / 'build-assets'),
               '--apt-mirror', 'https://mirrors.ustc.edu.cn/ubuntu',
               '--retry-status', 'environment_error', '--extra-instance-id', last,
               '--start', '200', '--stop', '300', '--additional', '0', '--clean-test-environment']
    (OUTPUT / 'launch-command.json').write_text(json.dumps(command, indent=2))
    with (OUTPUT / 'nohup.out').open('w') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (OUTPUT / 'batch.pid').write_text(str(process.pid) + '\n')
    print(json.dumps({'pid': process.pid, 'output': str(OUTPUT), 'scheduled': 80,
                      'parallel_cases': 2, 'last_instance': last, 'reused_results': 22}, indent=2))


if __name__ == '__main__':
    main()
