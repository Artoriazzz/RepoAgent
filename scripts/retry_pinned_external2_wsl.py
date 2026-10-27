"""修复两道直接注入实验的外部故障，保持冻结模型、提示词和经验不变。"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from run_pinned20_wsl import SWE, WORK, LIVE, HELPERS, digest, memory_hashes, save_json


PREVIOUS = WORK / 'runs/lite-pinned20-pass1-glm-20261025'
OUTPUT = WORK / 'runs/lite-pinned20-external2-retry-glm-20261025'
PREFLIGHT = WORK / 'pinned20-external2-preflight-20261025'
IDS = ['pylint-dev__pylint-7228', 'pytest-dev__pytest-5692']
PROXY = 'http://127.0.0.1:7897'
BYPASS = 'localhost,127.0.0.1,::1,raw.githubusercontent.com,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn'


def preflight():
    import docker
    import pandas as pd
    from run_failed5_preflight_wsl import prepare_image, clean_cached_test_image
    from resume_lite_parallel_wsl import REPO_IMPORTS, cached_baseline_check

    config = json.loads((PREVIOUS / 'experiment.json').read_text())
    os.environ.update(NO_PROXY=BYPASS, no_proxy=BYPASS, SWEBENCH_RAW_URL_PREFIX='',
                      SWEBENCH_REPO_MIRROR_ROOT=str(WORK / 'repo_cache'),
                      SWEBENCH_DOCKER_BUILD_NETWORK='host', SWEBENCH_GIT_PROXY=PROXY)
    os.environ.update({key: PROXY for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')})
    rows = {r['instance_id']: r for r in pd.read_parquet(config['dataset']).to_dict('records')}
    client = docker.from_env()
    PREFLIGHT.mkdir(exist_ok=False)
    evidence = {}
    for instance in IDS:
        row = rows[instance]
        print(f'Preparing {instance}', flush=True)
        with (PREFLIGHT / f'{instance}.log').open('w') as log:
            if row['repo'] == 'pylint-dev/pylint':
                # 官方安装步骤不变，仅使用 pip 24 的 legacy editable 兼容路径。
                evidence[instance] = prepare_image(row, SWE, client, log, imports=REPO_IMPORTS[row['repo']],
                    proxy=PROXY, apt_mirror='https://mirrors.ustc.edu.cn/ubuntu', canonical=True, clean_tests=True)
            else:
                evidence[instance] = {'status': 'ready', **clean_cached_test_image(row, SWE, client, log, proxy=PROXY)}
            image = client.images.get(f'sweb.eval.x86_64.{instance}:latest')
            check = cached_baseline_check(row, image.labels) + REPO_IMPORTS[row['repo']]
            log.write(client.containers.run(image.id, ['/opt/miniconda3/envs/testbed/bin/python', '-c', check], remove=True).decode())
            evidence[instance]['image_id'] = image.id
    save_json(PREFLIGHT / 'environments.json', evidence)
    client.close()
    print('Both baselines and imports passed.', flush=True)


def launch():
    config = json.loads((PREVIOUS / 'experiment.json').read_text())
    previous = json.loads((PREVIOUS / 'results.json').read_text())
    failed = {r['instance_id']: r for r in previous if r['instance_id'] in IDS}
    if set(failed) != set(IDS) or any(r['status'] not in {'environment_error', 'api_error'} for r in failed.values()):
        raise ValueError('Only the two external-error cases may be retried')
    source = Path(config['source_root'])
    for name, sha in config['source_sha256'].items():
        if digest(source / name) != sha:
            raise ValueError(f'Frozen source changed: {name}')
    # 环境预检完成后才启动付费模型调用。
    subprocess.run([str(SWE / '.venv/bin/python'), str(Path(__file__).resolve()), '--preflight'], check=True, cwd=SWE)
    OUTPUT.mkdir(exist_ok=False)
    runner = OUTPUT / 'runner'
    runner.mkdir()
    for name in HELPERS + ['run_pinned20_wsl.py', Path(__file__).name]:
        shutil.copyfile(Path(__file__).with_name(name), runner / name)
    # 沿用原诊断入口，防止重跑时悄然改变注入格式。
    shutil.copyfile(PREVIOUS / 'runner/pinned_memory_agent.py', runner / 'pinned_memory_agent.py')
    (runner / 'pinned_memory_agent.py').chmod(0o755)
    selection = json.loads((PREVIOUS / 'selection.json').read_text())
    selection['instances'] = [r for r in selection['instances'] if r['id'] in IDS]
    manifest = json.loads((PREVIOUS / 'memory-manifest.json').read_text())
    manifest['instances'] = {k: v for k, v in manifest['instances'].items() if k in IDS}
    for instance in IDS:
        shutil.copytree(PREVIOUS / 'memory' / instance, OUTPUT / 'memory' / instance)
        if memory_hashes(OUTPUT / 'memory' / instance) != config['memory_sha256'][instance]:
            raise ValueError(f'Frozen memories changed: {instance}')
    save_json(OUTPUT / 'selection.json', selection)
    save_json(OUTPUT / 'memory-manifest.json', manifest)
    config.update(previous_run=str(PREVIOUS), target_ids=IDS, network_no_proxy=BYPASS,
                  fresh_generation=True, environment_preflight=str(PREFLIGHT),
                  runner_sha256={p.name: digest(p) for p in runner.iterdir()})
    save_json(OUTPUT / 'experiment.json', config)
    shutil.copyfile(PREFLIGHT / 'environments.json', OUTPUT / 'environment-repair.json')
    (OUTPUT / 'work').mkdir()
    (OUTPUT / 'work/repo_cache').symlink_to(WORK / 'repo_cache', target_is_directory=True)
    with (OUTPUT / 'nohup.out').open('w') as log:
        process = subprocess.Popen([str(SWE / '.venv/bin/python'), '-u', str(runner / 'run_pinned20_wsl.py'),
                                    '--worker', '--output-dir', str(OUTPUT)], cwd=SWE, stdout=log,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    (OUTPUT / 'batch.pid').write_text(str(process.pid) + '\n')
    print(json.dumps({'pid': process.pid, 'output': str(OUTPUT), 'instances': IDS}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true')
    if parser.parse_args().preflight:
        preflight()
    else:
        launch()
