"""仅重跑向量检索实验的三道流程异常题，保留其余 32 题结果。"""

import json
import os
from pathlib import Path
import shutil
import subprocess

from run_pinned20_wsl import SWE, WORK, digest, memory_hashes, save_json


PREVIOUS = WORK / 'runs/lite-vector35-pass1-glm-20261026'
OUTPUT = WORK / 'runs/lite-vector35-external3-retry-glm-20261026'
IDS = ['matplotlib__matplotlib-26020', 'psf__requests-3362', 'mwaskom__seaborn-3190']
PROXY = 'http://127.0.0.1:7897'
BYPASS = 'localhost,127.0.0.1,::1,raw.githubusercontent.com,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn'


def preflight():
    import docker
    import pandas as pd
    from run_failed5_preflight_wsl import prepare_image, clean_cached_test_image
    from resume_lite_parallel_wsl import REPO_IMPORTS, cached_baseline_check

    config = json.loads((OUTPUT / 'experiment.json').read_text())
    rows = {r['instance_id']: r for r in pd.read_parquet(config['dataset']).to_dict('records')}
    directory = OUTPUT / 'preflight'
    directory.mkdir()
    client = docker.from_env()
    evidence = {}
    try:
        for instance in IDS:
            save_json(OUTPUT / 'progress.json', {'phase': 'environment_repair', 'instance_id': instance,
                                                'completed': len(evidence), 'total': 3})
            print(f'Preparing {instance}', flush=True)
            row = rows[instance]
            with (directory / f'{instance}.log').open('w') as log:
                if instance != 'mwaskom__seaborn-3190':
                    # 废弃错误的旧构建上下文，按官方 spec 与原始 base_commit 重建。
                    evidence[instance] = prepare_image(
                        row, SWE, client, log, imports=REPO_IMPORTS[row['repo']], proxy=PROXY,
                        apt_mirror='https://mirrors.ustc.edu.cn/ubuntu', canonical=True,
                        build_assets=WORK / 'build-assets', clean_tests=True)
                else:
                    evidence[instance] = {'status': 'ready', 'cached': True}
                evidence[instance].update(clean_cached_test_image(row, SWE, client, log, proxy=PROXY))
                image = client.images.get(f'sweb.eval.x86_64.{instance}:latest')
                script = cached_baseline_check(row, image.labels) + REPO_IMPORTS[row['repo']]
                log.write(client.containers.run(image.id, ['/opt/miniconda3/envs/testbed/bin/python', '-c', script],
                                                 working_dir='/testbed', remove=True).decode())
                evidence[instance]['image_id'] = image.id
            save_json(OUTPUT / 'environment-repair.json', evidence)
            print(f'Environment ready: {instance}', flush=True)
    finally:
        client.close()


def launch():
    config = json.loads((PREVIOUS / 'experiment.json').read_text())
    old_results = {r['instance_id']: r for r in json.loads((PREVIOUS / 'results.json').read_text())}
    if any(old_results[i]['status'] not in ('environment_error', 'agent_error') for i in IDS):
        raise ValueError('Only failed environment/query cases may be retried')
    for name, expected in config['source_sha256'].items():
        if digest(Path(config['source_root']) / name) != expected:
            raise ValueError(f'Frozen repair source changed: {name}')
    for name, expected in config['corpus_sha256'].items():
        if digest(Path(config['corpus']) / name) != expected:
            raise ValueError(f'Frozen memory corpus changed: {name}')
    OUTPUT.mkdir(exist_ok=False)
    runner = OUTPUT / 'runner'
    shutil.copytree(PREVIOUS / 'runner', runner, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # 唯一检索侧变化是一次 JSON 格式修正；排序、向量、经验与修复提示词不变。
    shutil.copy2(Path(__file__).with_name('memory_query.py'), runner / 'memory_query.py')
    shutil.copy2(Path(__file__), runner / Path(__file__).name)
    selection = json.loads((PREVIOUS / 'selection.json').read_text())
    selection['instances'] = [r for r in selection['instances'] if r['id'] in IDS]
    save_json(OUTPUT / 'selection.json', selection)
    for instance in IDS:
        shutil.copytree(PREVIOUS / 'memory' / instance, OUTPUT / 'memory' / instance)
        if memory_hashes(OUTPUT / 'memory' / instance) != config['memory_sha256'][instance]:
            raise ValueError(f'Frozen memories changed: {instance}')
    config.update(previous_run=str(PREVIOUS), target_ids=IDS, fresh_generation=True,
                  experiment='vector35_environment_and_query_format_retry3', network_no_proxy=BYPASS,
                  runner_sha256={p.name: digest(p) for p in runner.iterdir() if p.is_file()})
    save_json(OUTPUT / 'experiment.json', config)
    (OUTPUT / 'work').mkdir()
    (OUTPUT / 'work/repo_cache').symlink_to(WORK / 'repo_cache', target_is_directory=True)
    preflight()
    subprocess.run([str(SWE / '.venv/bin/python'), '-u', str(runner / 'run_vector35_wsl.py'),
                    '--worker', '--output', str(OUTPUT)], cwd=SWE, check=True)


if __name__ == '__main__':
    os.environ.update(NO_PROXY=BYPASS, no_proxy=BYPASS, SWEBENCH_RAW_URL_PREFIX='',
                      SWEBENCH_REPO_MIRROR_ROOT=str(WORK / 'repo_cache'),
                      SWEBENCH_DOCKER_BUILD_NETWORK='host', SWEBENCH_GIT_PROXY=PROXY,
                      HF_HUB_OFFLINE='1', HF_DATASETS_OFFLINE='1')
    os.environ.update({name: PROXY for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')})
    launch()
