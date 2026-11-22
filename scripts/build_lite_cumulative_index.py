"""Verify historical success provenance and build one deduplicated Lite index."""
import json
import re
from pathlib import Path

ROOT = Path('/home/artoria/workspace/SWE-bench/agent_work/runs')
OUT = ROOT / 'lite-cumulative-194-audit-20261122'
OUT.mkdir(parents=True, exist_ok=True)
entries = {}
missing = []


def load(path):
    return json.loads(path.read_text())


def add(instance_id, evidence, stage):
    row = entries.setdefault(instance_id, {'instance_id': instance_id, 'evidence': [], 'stage': stage})
    row['evidence'].append(str(evidence))


history = Path('/tmp/repoagent-historical154.md').read_text()
for line in history.splitlines():
    if not line.startswith('| `'):
        continue
    instance_id = re.findall(r'`([^`]+)`', line)[0]
    runs = re.findall(r'`([^`]+)`', line)[1:]
    found = False
    for run in runs:
        directory = ROOT / run
        for path in directory.glob('**/' + instance_id + '/summary.json'):
            if load(path).get('resolved') is True:
                add(instance_id, path, 'historical154')
                found = True
        if 'parser-reeval' in run:
            for path in directory.glob('*.json'):
                data = load(path)
                if isinstance(data, dict) and instance_id in data.get('resolved_ids', []):
                    add(instance_id, path, 'historical154')
                    found = True
    if not found:
        missing.append(instance_id)
assert len(entries) + len(missing) == 154

selection = load(Path('/tmp/repoagent-lite-remaining119-selection.json'))
for item in selection['diagnostic_resolved']:
    path = ROOT / item['summary_path']
    assert load(path).get('resolved') is True, path
    add(item['instance_id'], path, 'diagnostic7')
for run in ['lite-remaining119-public-behavior-20261119',
            'lite-remaining119-public-behavior-resume-20261120',
            'lite119-infra-recovery-20261121']:
    for path in (ROOT / run / 'cases').glob('*/results/*/summary.json'):
        data = load(path)
        if data.get('resolved') is True:
            add(data['instance_id'], path, 'recovery_to171')

ledger = load(ROOT / 'lite42-environment-ledger-20261122.json')
for instance_id, row in ledger['instances'].items():
    if row['resolved']:
        path = Path(row['result_path'])
        data = load(path)
        assert data['resolved'] and not data['error']
        add(instance_id, path, 'environment_recovery42')

result = {'count': len(entries), 'expected_count': 194, 'missing_historical_evidence': missing,
          'single_version_score': False, 'instances': entries}
(OUT / 'resolved-index.json').write_text(json.dumps(result, indent=2))
print(json.dumps({'verified_unique_successes': len(entries), 'missing': missing,
                  'index': str(OUT / 'resolved-index.json')}, indent=2))
assert not missing and len(entries) == 194
