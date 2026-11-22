"""Compare the unchanged baseline and saved patch without changing timing rules."""
import json
import sys
from pathlib import Path

sys.path.extend(str(p) for p in Path('/home/artoria/workspace/RepoAgent/RepoAgent/.venv/lib').glob('python*/site-packages'))
import docker

ROOT = Path('/home/artoria/workspace/SWE-bench/agent_work/runs')
OUT = ROOT / 'lite42-matplotlib23913-timing-audit'
OUT.mkdir(parents=True, exist_ok=True)
prediction_path = ROOT / 'lite-envfix-matplotlib-matplotlib-23913-rebuilt-20261122/evaluation/predictions.json'
predictions = json.loads(prediction_path.read_text())
if isinstance(predictions, dict):
    predictions = list(predictions.values()) if 'model_patch' not in predictions else [predictions]
patch = predictions[0]['model_patch']
program = '''
import functools,json,os,subprocess,time
if os.environ['APPLY_PATCH'] == '1':
    subprocess.run(['git','apply','-'], input=os.environ['SAVED_PATCH'], text=True, check=True)
import matplotlib.legend,pytest
original = matplotlib.legend.Legend._find_best_position
observations = []
@functools.wraps(original)
def measure(self,*args,**kwargs):
    start = time.perf_counter()
    try:
        return original(self,*args,**kwargs)
    finally:
        observations.append({'seconds':time.perf_counter()-start,'default_loc':self._loc_used_default})
matplotlib.legend.Legend._find_best_position = measure
code = pytest.main(['-q','lib/matplotlib/tests/test_legend.py::test_warn_big_data_best_loc'])
print('TIMING_AUDIT='+json.dumps({'exit_code':int(code),'observations':observations}))
'''
client = docker.from_env(timeout=300)
results = []
for mode in ['baseline', 'patched']:
    for repeat in range(3):
        container = client.containers.create('sweb.eval.x86_64.matplotlib__matplotlib-23913:latest',
            ['/opt/miniconda3/envs/testbed/bin/python', '-c', program],
            environment={'APPLY_PATCH':str(int(mode == 'patched')), 'SAVED_PATCH':patch},
            working_dir='/testbed', network_disabled=True)
        try:
            container.start()
            status = container.wait(timeout=180)
            log = container.logs().decode(errors='replace')
            path = OUT / f'{mode}-{repeat}.log'
            path.write_text(log)
            data = json.loads(next(line.split('=',1)[1] for line in log.splitlines() if line.startswith('TIMING_AUDIT=')))
            results.append(dict(mode=mode, repeat=repeat, log=str(path), **data))
        finally:
            container.remove(force=True)
        (OUT / 'results.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results,indent=2))
