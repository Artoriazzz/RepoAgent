"""Resume remaining saved-patch environment checks without model feedback."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path('/home/artoria/workspace/SWE-bench/agent_work/runs')
OUT = ROOT / 'lite42-environment-followup-20261122'
IDS = ['matplotlib__matplotlib-24334', 'matplotlib__matplotlib-25079',
       'matplotlib__matplotlib-26011', 'psf__requests-1963',
       'psf__requests-2148', 'psf__requests-2317', 'psf__requests-2674']
OUT.mkdir(parents=True, exist_ok=True)
progress = OUT / 'progress.json'
state = json.loads(progress.read_text()) if progress.exists() else {
    'selected': IDS, 'completed': [], 'errors': [], 'current': None}
for instance_id in IDS:
    name = 'lite42-followup-' + instance_id.replace('__', '-')
    report = ROOT / name / 'result.json'
    if any(x['instance_id'] == instance_id for x in state['completed']):
        continue
    if shutil.disk_usage('/mnt/d').free < 12 * 1024**3:
        state['stop_reason'] = 'D: below 12 GiB'
        break
    state['current'] = instance_id
    progress.write_text(json.dumps(state, indent=2))
    with (OUT / (instance_id + '.log')).open('w') as log:
        result = subprocess.run([sys.executable, str(OUT / 'reevaluate_lite_environment.py'),
            '--instance-id', instance_id, '--output-name', name],
            stdout=log, stderr=subprocess.STDOUT, timeout=2400)
    if report.exists() and result.returncode == 0:
        data = json.loads(report.read_text())
        state['completed'].append({'instance_id': instance_id,
            'resolved': data.get('resolved'), 'error': data.get('error'), 'run': name})
    else:
        state['errors'].append({'instance_id': instance_id, 'exit_code': result.returncode})
    progress.write_text(json.dumps(state, indent=2))
state['current'] = None
progress.write_text(json.dumps(state, indent=2))
if len(state['completed']) + len(state['errors']) == len(IDS):
    (OUT / 'finished.txt').touch()
