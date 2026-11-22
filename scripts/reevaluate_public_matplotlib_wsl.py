"""清理测试依赖污染，保留已有 patch 并单独重评七道 Matplotlib 题。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from audit_public_tests_wsl import final_patch


SWE = Path('/home/artoria/workspace/SWE-bench')
TAG = 'public-v12-testdeps-infra2-20261101'


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def main():
    import docker
    import pandas as pd

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--image-tag', default=TAG)
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    shutil.copyfile(__file__, output / f'orchestration-{script_hash}.py')
    selected = [x for x in json.loads((args.source_run / 'experiment.json').read_text())['instances']
                if x['repo'] == 'matplotlib/matplotlib']
    rows = pd.read_parquet(SWE / 'test-00000-of-00001.parquet').set_index('instance_id')
    helper = Path(__file__).with_name('repair_swebench_test_environment.py')
    if not helper.exists():
        helper = args.source_run / 'source/scripts/repair_swebench_test_environment.py'
    client = docker.from_env(timeout=300)
    predictions, dataset, manifest = [], [], {}
    python = '/opt/miniconda3/envs/testbed/bin/python'
    for item in selected:
        instance = item['id']
        case = output / instance
        case.mkdir(exist_ok=True)
        (output / 'progress.txt').write_text(f'preparing {instance}\n')
        row = rows.loc[instance].to_dict()
        row['instance_id'] = instance
        dataset.append(row)
        old = args.source_run / 'cases' / instance / 'results' / instance
        patch = final_patch(old / 'summary.json')
        if not patch.strip():
            raise ValueError(f'No saved patch for {instance}')
        predictions.append({'instance_id': instance, 'model_name_or_path': 'repoagent-glm-5.3-flash',
                            'model_patch': patch})
        base = client.images.get(f'sweb.eval.x86_64.{instance}:latest')
        if base.labels.get('repoagent.base-commit') != row['base_commit']:
            raise ValueError(f'Unexpected baseline image for {instance}')
        shutil.copyfile(helper, case / 'repair.py')
        dockerfile = (f'FROM {base.id}\n'
                      'ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple '
                      'PIP_DEFAULT_TIMEOUT=30 PIP_RETRIES=2\n'
                      'COPY repair.py /opt/repoagent/repair.py\n'
                      f'RUN {python} /opt/repoagent/repair.py '
                      '--constraints /opt/repoagent/test-constraints.txt\n'
                      'ENV PIP_CONSTRAINT=/opt/repoagent/test-constraints.txt '
                      'PIP_BUILD_CONSTRAINT=/opt/repoagent/test-constraints.txt\n')
        (case / 'Dockerfile').write_text(dockerfile)
        image = f'sweb.eval.x86_64.{instance}:{args.image_tag}'
        with (case / 'build.log').open('w', buffering=1) as log:
            for event in client.api.build(path=str(case), tag=image, rm=True, forcerm=True,
                                          network_mode='host', decode=True):
                log.write(event.get('stream', ''))
                if event.get('error'):
                    raise RuntimeError(event['error'])
        # 只验证无 patch 基线的公开运行能力，不改变 warning 规则或测试源码。
        smoke = (f"git diff --exit-code {row['base_commit']} -- . && {python} -c "
                 "\"import importlib.metadata as metadata; "
                 "assert metadata.version('setuptools-scm') == '7.1.0'; "
                 "assert not any(d.metadata['Name'].lower() == 'vcs-versioning' for d in metadata.distributions()); "
                 "import numpy,pandas,matplotlib; matplotlib.use('Agg'); "
                 "import matplotlib.pyplot as plt; plt.figure().savefig('/tmp/smoke.png'); "
                 "print(numpy.__version__,pandas.__version__,matplotlib.__version__)\"")
        container = client.containers.create(image, ['bash', '-lc', smoke], network_disabled=True)
        try:
            container.start()
            status = container.wait(timeout=180)['StatusCode']
            smoke_log = case / 'baseline-smoke.log'
            if smoke_log.exists():
                shutil.copyfile(smoke_log, case / 'baseline-smoke.previous.log')
            smoke_log.write_bytes(container.logs())
            if status:
                raise RuntimeError(f'Baseline smoke failed for {instance}')
        finally:
            container.remove(force=True)
        manifest[instance] = {'original_summary': str(old / 'summary.json'), 'base_image_id': base.id,
                              'image': image, 'image_id': client.images.get(image).id,
                              'patch_sha256': hashlib.sha256(patch.encode()).hexdigest(),
                              'repair_sha256': hashlib.sha256(helper.read_bytes()).hexdigest(),
                              'baseline_smoke_exit_code': status}
        save(output / 'environment.json', manifest)
        print(f'{instance}: baseline environment ready', flush=True)
    save(output / 'predictions.json', predictions)
    # pandas 负责转换数据集标量，避免 numpy 类型不能被 json 序列化。
    pd.DataFrame(dataset).to_json(output / 'dataset.json', orient='records')
    env = {**os.environ, 'SWEBENCH_DOCKER_NETWORK': 'host',
           'SWEBENCH_REPO_MIRROR_ROOT': str(SWE / 'agent_work/repo_cache'),
           'PYTHONUNBUFFERED': '1'}
    (output / 'progress.txt').write_text('evaluating seven unchanged patches, serial Harness\n')
    command = [sys.executable, '-m', 'swebench.harness.run_evaluation',
               '--dataset_name', str(output / 'dataset.json'),
               '--predictions_path', str(output / 'predictions.json'), '--max_workers', '1',
               '--timeout', '900', '--namespace', 'none', '--instance_image_tag', args.image_tag,
               '--cache_level', 'instance', '--run_id', output.name, '--report_dir', str(output)]
    result = subprocess.run(command, cwd=SWE, env=env)
    reports = []
    for instance in manifest:
        path = SWE / 'logs/run_evaluation' / output.name / 'repoagent-glm-5.3-flash' / instance / 'report.json'
        if path.exists():
            report = json.loads(path.read_text())[instance]
            reports.append({'instance_id': instance, 'resolved': report['resolved'],
                            'tests_status': {k: {s: len(v) for s, v in statuses.items()}
                                             for k, statuses in report['tests_status'].items()},
                            'report_path': str(path)})
    save(output / 'summary.json', {'source_run': str(args.source_run), 'reused_patches': True,
                                 'harness_exit_code': result.returncode, 'cases': reports})
    (output / 'finished.txt').write_text(f'exit_code={result.returncode}; reports={len(reports)}/7\n')
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
