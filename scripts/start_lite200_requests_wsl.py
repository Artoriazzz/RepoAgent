"""两路执行 Lite [200, 300)，并重评两道 Requests 的已有首轮产物。"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path('/home/artoria/workspace/SWE-bench')
RUNS = ROOT / 'agent_work/runs'
PREVIOUS = RUNS / 'external3-continuation-glm-20261023'
ORIGINALS = RUNS / 'infra10-network-retry-glm-20261023'
OUTPUT = RUNS / 'lite200to299-requests-network-glm-20261024'
REQUESTS = ['psf__requests-2148', 'psf__requests-2317']
HOSTS = {'www.google.co.uk': '127.77.0.2', 'www.google.com.hk': '127.77.0.3',
         'www.google.com': '127.77.0.4', 'httpbin.org': '127.77.0.5'}


def main():
    import docker

    config = json.loads((PREVIOUS / 'experiment.json').read_text())
    source = Path(config['source_root'])
    for name, digest in config['source_sha256'].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Frozen source changed: {name}')
    smoke = json.loads(Path('/tmp/requests-network-smoke.json').read_text())
    if {r['instance'] for r in smoke} != set(REQUESTS) or any(r['status']['StatusCode'] for r in smoke):
        raise RuntimeError('Requests network smoke tests did not pass')
    originals = [r for r in json.loads((ORIGINALS / 'results.json').read_text())
                 if r['instance_id'] in REQUESTS]
    if len(originals) != 2:
        raise RuntimeError('Missing saved Requests initial generations')
    OUTPUT.mkdir(exist_ok=False)
    network = OUTPUT / 'network'
    network.mkdir()
    for filename in ('requests_tcp_routes.py', 'requests-network-smoke.json', 'repoagent-route-probe.json'):
        shutil.copyfile(Path('/tmp') / filename, network / filename)
    client = docker.from_env(timeout=120)
    # 只替换本次创建的临时转发容器，改为挂载持久实验目录。
    service = client.containers.get('repoagent-requests-tcp-routes-20261024')
    service.remove(force=True)
    service = client.containers.run('sweb.eval.x86_64.psf__requests-2317:latest',
        ['/opt/miniconda3/envs/testbed/bin/python', '-u', '/routes.py'],
        name='repoagent-requests-tcp-routes-20261024', network_mode='host', user='root',
        detach=True, read_only=True, mem_limit='128m', cap_drop=['ALL'], cap_add=['NET_BIND_SERVICE'],
        volumes={str(network / 'requests_tcp_routes.py'): {'bind': '/routes.py', 'mode': 'ro'}})
    (network / 'container-id.txt').write_text(service.id + '\n')
    (network / 'extra-hosts.json').write_text(json.dumps(HOSTS, indent=2))
    shutil.copyfile(ROOT / 'swebench/harness/docker_build.py', network / 'docker_build.py')
    runner = OUTPUT / 'runner'
    shutil.copytree(ORIGINALS / 'runner', runner, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(Path(__file__).with_name('resume_lite_parallel_wsl.py'), runner / 'resume_lite_parallel_wsl.py')
    baseline = OUTPUT / 'baseline'
    baseline.mkdir()
    (baseline / 'experiment.json').write_text(json.dumps(config, indent=2))
    (baseline / 'results.json').write_text(json.dumps(originals, indent=2))
    bypass = 'localhost,127.0.0.1,::1,open.bigmodel.cn,raw.githubusercontent.com,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn'
    env = {**os.environ, 'REPO_AGENT_NETWORK_NO_PROXY': bypass,
           'NO_PROXY': bypass, 'no_proxy': bypass,
           'REPO_AGENT_LLM_TIMEOUT_SECONDS': '600', 'REPO_AGENT_LLM_MAX_RETRIES': '2',
           'REPO_AGENT_REVIEW_MAX_OUTPUT_TOKENS': '12288'}
    command = [str(ROOT / '.venv/bin/python'), '-u', str(runner / 'resume_lite_parallel_wsl.py'),
               '--previous-run', str(baseline), '--source-root', str(source), '--output-dir', str(OUTPUT),
               '--wheelhouse', str(ROOT / 'agent_work/build-wheelhouse'),
               '--build-assets', str(ROOT / 'agent_work/build-assets'),
               '--apt-mirror', 'https://mirrors.ustc.edu.cn/ubuntu',
               '--start', '200', '--stop', '300', '--additional', '0', '--clean-test-environment',
               '--requests-extra-hosts', str(network / 'extra-hosts.json')]
    for instance in REQUESTS:
        command += ['--extra-instance-id', instance, '--reuse-initial-instance-id', instance]
    (OUTPUT / 'launch-command.json').write_text(json.dumps(command, indent=2))
    with (OUTPUT / 'nohup.out').open('w') as log:
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (OUTPUT / 'batch.pid').write_text(str(proc.pid) + '\n')
    print(json.dumps({'pid': proc.pid, 'output': str(OUTPUT), 'total': 102, 'parallel_cases': 2}, indent=2))


if __name__ == '__main__':
    main()
