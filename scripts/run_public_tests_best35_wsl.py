"""使用冻结代码运行固定名单，环境准备串行、任务最多两路并行。"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import traceback


SWE = Path('/home/artoria/workspace/SWE-bench')
PYTHON = Path('/home/artoria/workspace/RepoAgent/RepoAgent/.venv/bin/python')
HARNESS = SWE / '.venv/bin/python'
DATASET = SWE / 'test-00000-of-00001.parquet'
ENV = Path('/home/artoria/workspace/RepoAgent/RepoAgent/.env')
SELECTION = SWE / 'agent_work/runs/lite-vector35-pass1-glm-20261026/selection.json'
IMPORTS = {
    'django/django': 'import django; from django.db import models',
    'matplotlib/matplotlib': 'import matplotlib; import matplotlib.pyplot',
    'mwaskom/seaborn': 'import seaborn',
    'pallets/flask': 'import flask',
    'psf/requests': 'import requests',
    'pydata/xarray': 'import xarray',
    'pylint-dev/pylint': 'import pylint, astroid',
    'pytest-dev/pytest': 'import pytest, _pytest',
    'scikit-learn/scikit-learn': 'import sklearn; from sklearn.utils.validation import check_array',
    'sphinx-doc/sphinx': 'import sphinx, docutils, roman; import sphinx.testing.fixtures',
    'sympy/sympy': 'import sympy, mpmath; import sympy.utilities.runtests',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(path)


def credit_exhausted(summary):
    for error in summary.get('api_errors', []):
        if error.get('type') != 'RateLimitError':
            continue
        output = Path(error['log_path']).read_text(errors='replace')[-12000:]
        if '余额不足' in output or re.search(r'''["']code["']\s*:\s*["']1113["']''', output):
            return True
    return False


def baseline_check_script(row, imports):
    # 已核实的 Harness 安装提交只修改这两个配置文件；生产代码仍须匹配基线。
    allowed = {'setup.py', 'tox.ini'} if (
        row['repo'] == 'sphinx-doc/sphinx' and str(row.get('version')) in {'3.1', '3.3', '3.4'}
    ) else set()
    return (
        'import subprocess,json; '
        f'base={row["base_commit"]!r}; '
        'subprocess.run(["git","diff","--exit-code","HEAD","--","."],check=True); '
        'subprocess.run(["git","merge-base","--is-ancestor",base,"HEAD"],check=True); '
        'changed=subprocess.check_output(["git","diff","--name-only",base,"--","."],universal_newlines=True).splitlines(); '
        f'assert set(changed) <= {allowed!r}, changed; '
        + imports + '; '
        'print(json.dumps({"base_commit":base,"runtime_head":subprocess.check_output('
        '["git","rev-parse","HEAD"],universal_newlines=True).strip(),"setup_changes":changed,"imports_ok":True}))'
    )


def environment_dockerfile(row, dockerfile):
    lines = dockerfile.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith('FROM '))
    settings = ['ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple '
                'PIP_DEFAULT_TIMEOUT=30 PIP_RETRIES=2']
    if row['repo'] == 'scikit-learn/scikit-learn' and str(row.get('version')) == '1.3':
        # An empty value is unset on deactivation, breaking the next activation under nounset.
        settings.append('ENV QT_XCB_GL_INTEGRATION=none')
    lines[first + 1:first + 1] = settings
    return '\n'.join(lines) + '\n'


def prepare(source, instance, destination):
    import docker
    import pandas as pd

    sys.path.insert(0, str(source / 'scripts'))
    from run_failed5_preflight_wsl import clean_cached_test_image, prepare_image
    from swebench.harness.docker_build import build_image, get_env_configs_to_build
    from swebench.harness.test_spec.test_spec import make_test_spec

    os.environ['SWEBENCH_REPO_MIRROR_ROOT'] = str(SWE / 'agent_work/repo_cache')
    os.environ['SWEBENCH_DOCKER_BUILD_NETWORK'] = 'host'
    row = pd.read_parquet(DATASET).set_index('instance_id').loc[instance].to_dict()
    row['instance_id'] = instance
    imports = IMPORTS[row['repo']]
    client = docker.from_env()
    image_name = f'sweb.eval.x86_64.{instance}:latest'
    with (destination / 'environment.log').open('a', buffering=1) as log:
        try:
            image = client.images.get(image_name)
        except docker.errors.ImageNotFound:
            image = None
        if image is None or image.labels.get('repoagent.base-commit') != row['base_commit']:
            # 基础依赖也需要可用的下载链路；保留官方版本约束和安装脚本。
            spec = make_test_spec(row)
            configs = get_env_configs_to_build(client, [spec])
            for name, config in configs.items():
                directory = destination / 'environment-build' / name.replace(':', '__')
                directory.mkdir(parents=True, exist_ok=True)
                dockerfile = environment_dockerfile(row, config['dockerfile'])
                changes = ['host build network', 'pip download mirror, timeout and retries']
                if 'ENV QT_XCB_GL_INTEGRATION=' in dockerfile:
                    changes.append('define optional Qt activation variable in the dependency image')
                save(directory / 'provenance.json', {
                    'instance': instance, 'baseline': row['base_commit'],
                    'official_setup_sha256': hashlib.sha256(config['setup_script'].encode()).hexdigest(),
                    'official_dockerfile_sha256': hashlib.sha256(config['dockerfile'].encode()).hexdigest(),
                    'effective_dockerfile_sha256': hashlib.sha256(dockerfile.encode()).hexdigest(),
                    'changes': changes,
                })
                print(f'Building dependency image {name}', flush=True)
                build_image(name, {'setup_env.sh': config['setup_script']},
                            dockerfile, config['platform'], client, directory)
            assets = SWE / 'agent_work/build-assets'
            result = prepare_image(row, SWE, client, log, canonical=True, proxy='', imports=imports,
                                   build_assets=assets if row['repo'] == 'matplotlib/matplotlib'
                                   and assets.is_dir() else None,
                                   clean_tests=row['repo'] == 'matplotlib/matplotlib')
        else:
            result = {'status': 'ready', 'image': image_name, 'image_id': image.id,
                      'baseline': row['base_commit'], 'reused': True}
        # 复用已有的历史版本修复：pytest 标签，以及 Sphinx 3.1 测试依赖。
        if row['repo'] == 'pytest-dev/pytest' or (
            row['repo'] == 'sphinx-doc/sphinx' and str(row.get('version')) == '3.1'
        ):
            result.update(clean_cached_test_image(row, SWE, client, log, proxy=''))
        # 检查基线源码与依赖；不加载官方新增测试或答案补丁。
        output = client.containers.run(result['image'],
                                       ['/opt/miniconda3/envs/testbed/bin/python', '-c', baseline_check_script(row, imports)],
                                       working_dir='/testbed', network_disabled=True, remove=True)
        log.write(output.decode())
        result['baseline_check'] = json.loads(output)
    save(destination / 'environment-ready.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--selection', type=Path, default=SELECTION)
    parser.add_argument('--limit', type=int, default=35)
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2)
    parser.add_argument('--prepare-instance')
    args = parser.parse_args()
    run = args.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=True)
    if args.prepare_instance:
        prepare(args.source, args.prepare_instance, run)
        return 0
    source = run / 'source'
    if not source.exists():
        shutil.copytree(args.source, source,
                        ignore=shutil.ignore_patterns('.git', '.venv', '__pycache__', '.env', '.env.*'))
    selected = json.loads(args.selection.read_text())['instances'][:args.limit]
    if len(selected) != args.limit or any(x['repo'] not in IMPORTS for x in selected):
        raise ValueError('Selection size or repository preflight is unsupported')
    total = len(selected)
    hashes = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in source.rglob('*.py')}
    manifest = run / 'experiment.json'
    if not manifest.exists():
        save(manifest, {'started_at': now(), 'source_origin': str(args.source),
             'source_hashes': hashes, 'selection_source': str(args.selection), 'instances': selected,
             'workers': args.workers, 'model': 'glm-5.3-flash', 'reasoning_effort': 'high',
             'max_steps': 24, 'memory_recall': False, 'feedback_source': 'public-tests',
             'final_harness_only': True, 'cache_level': 'instance',
             'temperature': 'unchanged v12 provider default',
             'launcher_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    else:
        prior = json.loads(manifest.read_text())
        if prior['source_hashes'] != hashes or prior['instances'] != selected:
            raise ValueError('Resume source or selection differs from frozen experiment')
    wrapper = run / 'repo-agent.sh'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(str(PYTHON)) + ' -m repo_agent "$@"\n')
    wrapper.chmod(0o755)
    states = {x['id']: {'index': i, 'status': 'queued'} for i, x in enumerate(selected, 1)}
    lock, build_lock = threading.Lock(), threading.Lock()
    quota_stop = threading.Event()

    def update(instance, **values):
        with lock:
            states[instance].update(values, updated_at=now())
            finished = sum(x['status'] in {'done', 'environment_error', 'runner_error'}
                           for x in states.values())
            save(run / 'progress.json', {'updated_at': now(), 'total': total,
                 'finished': finished, 'instances': states})
            print(f"[{states[instance]['index']}/{total}] {instance}: {states[instance]['status']}", flush=True)

    def execute(item):
        instance = item['id']
        if quota_stop.is_set():
            update(instance, status='waiting_for_api_credit')
            return
        case = run / 'cases' / instance
        case.mkdir(parents=True, exist_ok=True)
        summary = case / 'results' / instance / 'summary.json'
        resumed = summary.exists()
        try:
            if not summary.exists():
                with build_lock:
                    if quota_stop.is_set():
                        update(instance, status='waiting_for_api_credit')
                        return
                    update(instance, status='preparing_environment')
                    with (case / 'preflight.log').open('a') as log:
                        subprocess.run([str(HARNESS), str(Path(__file__).resolve()),
                            '--source', str(source), '--run-dir', str(case),
                            '--prepare-instance', instance], stdout=log, stderr=subprocess.STDOUT,
                            check=True, timeout=1800)
                if quota_stop.is_set():
                    update(instance, status='waiting_for_api_credit')
                    return
                update(instance, status='running')
                work = case / 'work'
                work.mkdir(exist_ok=True)
                if not (work / 'repo_cache').exists():
                    (work / 'repo_cache').symlink_to(SWE / 'agent_work/repo_cache', target_is_directory=True)
                env = os.environ.copy()
                env.update(PYTHONPATH=str(source), PATH='/home/artoria/.local/bin:' + env.get('PATH', ''),
                           SWEBENCH_REPO_MIRROR_ROOT=str(SWE / 'agent_work/repo_cache'))
                command = [str(PYTHON), '-u', str(source / 'scripts/swebench_batch.py'),
                    '--dataset', str(DATASET), '--repo-agent-bin', str(wrapper),
                    '--model', 'glm-5.3-flash', '--reasoning-effort', 'high',
                    '--coordinator-reasoning-effort', 'high', '--implement-reasoning-effort', 'high',
                    '--work-dir', str(work), '--output-dir', str(case / 'results'),
                    '--env-file', str(ENV), '--proxy', 'http://127.0.0.1:7897', '--offline-cache',
                    '--instance-id', instance, '--limit', '1', '--repair-mode', 'experience-build',
                    '--feedback-source', 'public-tests', '--no-repair-memory',
                    '--memory-root', str(case / 'memory'), '--memory-provenance', 'public-tests-' + run.name,
                    '--candidate-mode', 'off', '--max-steps', '24', '--timeout', '3600',
                    '--prebuild-instance-image', '--image-build-timeout', '1800', '--evaluate',
                    '--swebench-dir', str(SWE), '--harness-python', str(HARNESS), '--run-id', run.name,
                    '--harness-timeout', '900', '--cache-level', 'instance', '--namespace', 'none',
                    '--docker-network', 'host']
                with (case / 'run.log').open('a') as log:
                    result = subprocess.run(command, env=env, cwd=source,
                                            stdout=log, stderr=subprocess.STDOUT)
                if not summary.exists():
                    raise RuntimeError(f'Batch exited {result.returncode} without summary')
            data = json.loads(summary.read_text())
            if not resumed and credit_exhausted(data):
                quota_stop.set()
                save(run / 'credit-exhausted.json', {'instance_id': instance, 'summary': str(summary),
                                                    'updated_at': now(), 'pending_tasks_stopped': True})
            update(instance, status='done', agent_status=data.get('status'),
                   resolved=data.get('resolved'), seconds=data.get('seconds'), summary=str(summary), resumed=resumed)
        except Exception as exc:
            (case / 'exception.log').write_text(traceback.format_exc())
            update(instance, status='environment_error' if not (case / 'environment-ready.json').exists()
                   else 'runner_error', error=f'{type(exc).__name__}: {exc}')

    save(run / 'progress.json', {'total': total, 'finished': 0, 'instances': states})
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        list(executor.map(execute, selected))
    predictions = []
    for item in selected:
        path = run / 'cases' / item['id'] / 'results/predictions.json'
        if path.exists():
            predictions.extend(json.loads(path.read_text()))
    save(run / 'predictions.json', predictions)
    save(run / 'summary.json', list(states.values()))
    if quota_stop.is_set():
        (run / 'waiting-for-api-credit.txt').write_text(now() + '\n')
        return 2
    (run / 'finished.txt').write_text(now() + '\n')
    return int(any(x['status'] != 'done' for x in states.values()))


if __name__ == '__main__':
    raise SystemExit(main())
