"""使用同一冻结向量库和修复设置，执行纠正成功队列的剩余 36 题。"""

import json
from pathlib import Path
import shutil
import subprocess

from run_pinned20_wsl import SWE, WORK, digest, memory_hashes, save_json


FIRST = WORK / 'runs/lite-vector35-pass1-glm-20261026'
REFERENCE = WORK / 'runs/lite-vector35-external3-retry-glm-20261026'
OUTPUT = WORK / 'runs/lite-vector-remaining36-pass1-glm-20261026'


def remaining_cases(history, first_selection):
    used = {row['id'] for row in first_selection['instances']}
    rows = sorted([r for r in history if r['instance_id'] not in used], key=lambda r: r['dataset_index'])
    if len(history) != 71 or len(used) != 35 or len(rows) != 36:
        raise ValueError('Expected the disjoint remaining 36 cases from the same 71-case cohort')
    return rows


def main():
    from repo_agent.core.memory import MemoryStore

    config = json.loads((REFERENCE / 'experiment.json').read_text())
    for name, expected in config['source_sha256'].items():
        if digest(Path(config['source_root']) / name) != expected:
            raise ValueError(f'Frozen repair source changed: {name}')
    for name, expected in config['corpus_sha256'].items():
        if digest(Path(config['corpus']) / name) != expected:
            raise ValueError(f'Frozen memory corpus changed: {name}')
    if digest(Path(config['dataset'])) != config['dataset_sha256']:
        raise ValueError('Frozen dataset changed')
    rows = remaining_cases(json.loads((FIRST / 'historical_cases.json').read_text()),
                           json.loads((FIRST / 'selection.json').read_text()))
    OUTPUT.mkdir(exist_ok=False)
    runner = OUTPUT / 'runner'
    shutil.copytree(REFERENCE / 'runner', runner, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # 只更新环境构建开关；查询格式修正与修复入口沿用三题补跑冻结副本。
    shutil.copy2(Path(__file__).with_name('run_pinned20_wsl.py'), runner / 'run_pinned20_wsl.py')
    shutil.copy2(Path(__file__), runner / Path(__file__).name)
    records = json.loads((Path(config['corpus']) / 'records.json').read_text())
    hashes = {}
    for row in rows:
        root = OUTPUT / 'memory' / row['instance_id']
        store = MemoryStore.for_scope(row['repo'], root=root)
        for record in records:
            kind = record['memory_type']
            if kind != 'global_experience' and record['repository'] != row['repo']:
                continue
            original = MemoryStore.for_scope(record['repository'], root=Path(config['corpus']) / 'memory')
            directory = store.global_memory_dir if kind == 'global_experience' else store.memory_dir
            original_dir = original.global_memory_dir if kind == 'global_experience' else original.memory_dir
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original_dir / record['memory_filename'], directory / record['memory_filename'])
        store.update_index()
        hashes[row['instance_id']] = memory_hashes(root)
    save_json(OUTPUT / 'selection.json', {'instances': [
        {'id': row['instance_id'], 'index': row['dataset_index'], 'repo': row['repo']} for row in rows]})
    config.pop('previous_run', None)
    config.update(target_ids=[row['instance_id'] for row in rows], memory_sha256=hashes,
                  experiment='vector_top1_both_types_remaining36_corrected_successes',
                  first35_final_results=str(REFERENCE / 'combined_results.json'), canonical_environment_builds=True,
                  fresh_generation=True, feedback_correction=False, memory_write=False,
                  runner_sha256={p.name: digest(p) for p in runner.iterdir() if p.is_file()})
    save_json(OUTPUT / 'experiment.json', config)
    (OUTPUT / 'work').mkdir()
    (OUTPUT / 'work/repo_cache').symlink_to(WORK / 'repo_cache', target_is_directory=True)
    save_json(OUTPUT / 'progress.json', {'phase': 'repair_starting', 'completed': 0, 'total': 36})
    print(f'Starting remaining 36 cases; same frozen {len(records)} memories and vector index', flush=True)
    subprocess.run([str(SWE / '.venv/bin/python'), '-u', str(runner / 'run_vector35_wsl.py'),
                    '--worker', '--output', str(OUTPUT)], cwd=SWE, check=True)


if __name__ == '__main__':
    main()
