"""修复流中断与测试依赖后，复用首轮补丁并重跑两题的评测及纠正。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path('/home/artoria/workspace/SWE-bench')
WORK = ROOT / 'agent_work'
PREVIOUS = WORK / 'runs/lite200to299-env79-plus300-glm-20261024'
OUTPUT = WORK / 'runs/lite-external2-stream-env-retry-v2-glm-20261024'
INSTANCES = ['scikit-learn__scikit-learn-15512', 'sphinx-doc__sphinx-7686']


def main():
    config = json.loads((PREVIOUS / 'experiment.json').read_text())
    original = Path(config['source_root'])
    for name, digest in config['source_sha256'].items():
        if hashlib.sha256((original / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Frozen source changed: {name}')
    records = json.loads((PREVIOUS / 'results.json').read_text())
    records = [r for r in records if r['instance_id'] in INSTANCES]
    if len(records) != 2 or any(r.get('resolved') for r in records):
        raise RuntimeError('Expected the two unresolved cases')
    control = ROOT / 'environment-control.sphinx7686-env-control-v2-20261024.json'
    if not control.exists() or 'sphinx-doc__sphinx-7686' not in json.loads(control.read_text()).get('resolved_ids', []):
        raise RuntimeError('Sphinx reference-patch environment control has not passed')
    OUTPUT.mkdir(exist_ok=False)
    source = OUTPUT / 'source'
    shutil.copytree(original, source, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for src, dest in [('retry2-llm.py', 'repo_agent/core/llm.py'),
                      ('retry2-batch.py', 'scripts/swebench_batch.py')]:
        shutil.copyfile(Path('/tmp') / src, source / dest)
    updated = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
               for name in config['source_sha256']}
    baseline = OUTPUT / 'baseline'
    baseline.mkdir()
    baseline_config = {**config, 'source_root': str(source), 'source_sha256': updated,
                       'parent_source_sha256': config['source_sha256'], 'target_ids': INSTANCES}
    (baseline / 'experiment.json').write_text(json.dumps(baseline_config, indent=2))
    (baseline / 'results.json').write_text(json.dumps(records, indent=2))
    runner = OUTPUT / 'runner'
    shutil.copytree(PREVIOUS / 'runner', runner, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(Path(__file__).with_name('run_failed5_preflight_wsl.py'), runner / 'run_failed5_preflight_wsl.py')
    # 环境控制产物不复制到 Agent 的任务目录或记忆目录。
    shutil.copyfile(WORK / 'retry2-environment-check-20261024/sphinx-environment.json', OUTPUT / 'environment-repair.json')
    bypass = 'localhost,127.0.0.1,::1,open.bigmodel.cn,raw.githubusercontent.com,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn'
    env = {**os.environ, 'REPO_AGENT_NETWORK_NO_PROXY': bypass, 'NO_PROXY': bypass, 'no_proxy': bypass,
           'REPO_AGENT_LLM_TIMEOUT_SECONDS': '600', 'REPO_AGENT_LLM_MAX_RETRIES': '2',
           'REPO_AGENT_REVIEW_MAX_OUTPUT_TOKENS': '12288', 'HF_HUB_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1'}
    command = [str(ROOT / '.venv/bin/python'), '-u', str(runner / 'resume_lite_parallel_wsl.py'),
               '--previous-run', str(baseline), '--source-root', str(source), '--output-dir', str(OUTPUT),
               '--wheelhouse', str(WORK / 'build-wheelhouse'), '--build-assets', str(WORK / 'build-assets'),
               '--apt-mirror', 'https://mirrors.ustc.edu.cn/ubuntu', '--retry-unresolved',
               '--start', '200', '--stop', '300', '--additional', '0', '--clean-test-environment']
    for instance in INSTANCES:
        command += ['--reuse-initial-instance-id', instance]
    (OUTPUT / 'launch-command.json').write_text(json.dumps(command, indent=2))
    with (OUTPUT / 'nohup.out').open('w') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (OUTPUT / 'batch.pid').write_text(str(process.pid) + '\n')
    print(json.dumps({'pid': process.pid, 'output': str(OUTPUT), 'instances': INSTANCES,
                      'parallel_cases': 2, 'reuse_initial_patch': True}, indent=2))


if __name__ == '__main__':
    main()
