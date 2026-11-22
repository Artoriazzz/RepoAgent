"""Wait for the current lane, then verify saved Requests patches with system CA."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/home/artoria/workspace/SWE-bench/agent_work/runs')
connect = os.environ.get('REQUESTS_CONNECT_RELAY') == '1'
OUT = ROOT / ('lite42-requests-connect-20261122' if connect else 'lite42-requests-ca-20261122')
OUT.mkdir(parents=True, exist_ok=True)
state = {'stage': 'waiting_for_current_lane', 'completed': [], 'errors': []}
progress = OUT / 'progress.json'
progress.write_text(json.dumps(state, indent=2))
while subprocess.run(['kill', '-0', os.environ.get('WAIT_PID', '732049')], capture_output=True).returncode == 0:
    time.sleep(60)
state['stage'] = 'running'
for suffix in ['1963', '2148', '2317', '2674']:
    instance_id = 'psf__requests-' + suffix
    state['current'] = instance_id
    progress.write_text(json.dumps(state, indent=2))
    name = ('lite42-requests-connect-final-' if connect else 'lite42-requests-ca-') + suffix
    with (OUT / (instance_id + '.log')).open('w') as log:
        run = subprocess.run([sys.executable, str(OUT / 'reevaluate_lite_environment.py'),
            '--instance-id', instance_id, '--output-name', name] + (['--requests-connect-relay'] if connect else []),
            stdout=log, stderr=subprocess.STDOUT)
    result = ROOT / name / 'result.json'
    if run.returncode == 0 and result.exists():
        data = json.loads(result.read_text())
        state['completed'].append({'instance_id': instance_id, 'resolved': data.get('resolved'),
            'error': data.get('error'), 'result': str(result)})
    else:
        state['errors'].append({'instance_id': instance_id, 'exit_code': run.returncode})
    progress.write_text(json.dumps(state, indent=2))
state.update(stage='finished', current=None)
progress.write_text(json.dumps(state, indent=2))
(OUT / 'finished.txt').touch()
