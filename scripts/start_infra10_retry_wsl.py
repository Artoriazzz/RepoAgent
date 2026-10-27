"""修复下载链路后，沿用冻结的 Agent 重新运行十道外部故障题。"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path('/home/artoria/workspace/SWE-bench')
PREVIOUS = ROOT / 'agent_work/runs/unresolved41-state-contract-glm-20261023'
OUTPUT = ROOT / 'agent_work/runs/infra10-network-retry-glm-20261023'
INSTANCES = {
    'matplotlib__matplotlib-24334', 'matplotlib__matplotlib-26011',
    'psf__requests-1963', 'psf__requests-2148', 'pytest-dev__pytest-5103',
    'pytest-dev__pytest-5221', 'psf__requests-2317', 'pydata__xarray-3364',
    'pylint-dev__pylint-7228', 'pytest-dev__pytest-9359',
}


def main():
    config = json.loads((PREVIOUS / 'experiment.json').read_text())
    source = Path(config['source_root'])
    for name, digest in config['source_sha256'].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Frozen source changed: {name}')
    rows = json.loads((PREVIOUS / 'results.json').read_text())
    selected = [row for row in rows if row['instance_id'] in INSTANCES]
    if len(selected) != 10:
        raise ValueError('Expected exactly ten selected instances')
    OUTPUT.mkdir(exist_ok=False)
    runner = OUTPUT / 'runner'
    shutil.copytree(PREVIOUS / 'runner', runner, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('resume_lite_parallel_wsl.py', 'run_failed5_preflight_wsl.py'):
        shutil.copyfile(Path(__file__).with_name(name), runner / name)
    # 仅在新基线里更正状态分类，历史产物保持原样。
    for row in selected:
        if row['status'] == 'agent_error':
            stdout = Path(row['output_dir']) / 'pass-1/agent_stdout.txt'
            if 'finish_reason=network_error' not in stdout.read_text():
                raise ValueError(f'Network error not confirmed: {row["instance_id"]}')
            row['previous_status'] = row['status']
            row['status'] = 'api_error'
            row['retry_reason'] = 'Incomplete LLM stream: finish_reason=network_error'
    baseline = OUTPUT / 'baseline'
    baseline.mkdir()
    (baseline / 'experiment.json').write_text(json.dumps(config, indent=2))
    (baseline / 'results.json').write_text(json.dumps(rows, indent=2))
    (OUTPUT / 'instances.json').write_text(json.dumps(sorted(INSTANCES), indent=2))
    bypass = ','.join(['localhost', '127.0.0.1', '::1', 'open.bigmodel.cn',
                       'raw.githubusercontent.com', 'pypi.tuna.tsinghua.edu.cn',
                       'mirrors.ustc.edu.cn'])
    environment = {**os.environ, 'REPO_AGENT_NETWORK_NO_PROXY': bypass,
                   'REPO_AGENT_LLM_TIMEOUT_SECONDS': '600', 'REPO_AGENT_LLM_MAX_RETRIES': '2',
                   'NO_PROXY': bypass, 'no_proxy': bypass}
    command = [str(ROOT / '.venv/bin/python'), '-u', str(runner / 'resume_lite_parallel_wsl.py'),
               '--previous-run', str(baseline), '--source-root', str(source),
               '--output-dir', str(OUTPUT), '--wheelhouse', str(ROOT / 'agent_work/build-wheelhouse'),
               '--build-assets', str(ROOT / 'agent_work/build-assets'),
               '--apt-mirror', 'https://mirrors.ustc.edu.cn/ubuntu',
               '--retry-status', 'environment_error', '--retry-status', 'api_error',
               '--start', '110', '--stop', '200', '--additional', '0', '--clean-test-environment']
    with (OUTPUT / 'nohup.out').open('w') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (OUTPUT / 'batch.pid').write_text(str(process.pid) + '\n')
    print(json.dumps({'pid': process.pid, 'output': str(OUTPUT), 'cases': len(selected),
                      'parallel_cases': 2, 'source': str(source)}, indent=2), flush=True)


if __name__ == '__main__':
    main()
