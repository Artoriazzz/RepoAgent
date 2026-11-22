"""Build a provenance ledger; successful re-evaluations are deduplicated by ID."""
import json
from pathlib import Path

ROOT = Path('/home/artoria/workspace/SWE-bench/agent_work/runs')
audit = json.loads(Path('/tmp/repoagent-audit-lite129-result.json').read_text())['rows']
ids = {i for i, r in audit.items() if i.startswith(('pytest-dev__', 'psf__requests-'))
       or 'matplotlib_scm_warning' in r.get('signatures', [])
       or (i.startswith('sphinx-doc__') and r.get('patch_chars'))}
ids.update(['pallets__flask-4045', 'astropy__astropy-14182'])
assert len(ids) == 42
baseline_added = {'psf__requests-863'}
for path in ROOT.glob('pytest-version*/result.json'):
    data = json.loads(path.read_text())
    if data.get('resolved'):
        for i in ids:
            if i in data.get('run_id', ''):
                baseline_added.add(i)
assert len(baseline_added) == 11, baseline_added
ledger = {}
for i in sorted(ids):
    candidates = []
    patterns = ['lite-envfix-*/result.json', 'lite42-followup-*/result.json',
                'lite42-requests-ca-*/result.json', 'pytest-version*/result.json',
                'lite42-sphinx-*-verbose3/result.json',
                'lite42-requests-connect-*/result.json',
                'lite42-requests-service-v*-*/result.json',
                'lite42-astropy-build-constraint-*/result.json',
                'requests863-local-httpbin-20261122/result.json']
    for pattern in patterns:
        for path in ROOT.glob(pattern):
            data = json.loads(path.read_text())
            if i in data.get('run_id', ''):
                candidates.append((path.stat().st_mtime, path, data))
    candidates.sort(key=lambda x: x[0])
    success = [x for x in candidates if x[2].get('resolved') is True and not x[2].get('error')]
    selected = success[-1] if success else candidates[-1] if candidates else None
    row = {'instance_id': i, 'included_in_baseline182': i in baseline_added,
           'resolved': bool(success), 'reports': [str(x[1]) for x in candidates]}
    if selected:
        _, path, data = selected
        row.update(result_path=str(path), harness_error=data.get('error'),
                   test_output_path=data.get('test_output_path'))
        statuses = data.get('instance_report', {}).get('tests_status', {})
        row['tests_status'] = {k: {s: len(v) for s, v in statuses.get(k, {}).items()}
                               for k in ['FAIL_TO_PASS', 'PASS_TO_PASS']}
    ledger[i] = row
    row['outcome'] = 'resolved' if success else 'completed_unresolved'
    if i == 'matplotlib__matplotlib-23913':
        row['outcome'] = 'baseline_timing_sensitive_unresolved'
        row['baseline_control'] = str(ROOT / 'lite42-matplotlib23913-timing-audit/results.json')
new = sorted(i for i, r in ledger.items() if r['resolved'] and not r['included_in_baseline182'])
out = ROOT / 'lite42-environment-ledger-20261122.json'
out.write_text(json.dumps({'baseline': 182, 'scope': 42, 'new_resolved_ids': new,
    'cumulative_resolved': 182 + len(new), 'single_version_score': False,
    'note': 'Unresolved reports may retain environment failures; inspect logs before classifying as code failure.',
    'outcome_counts': {label: sum(r['outcome'] == label for r in ledger.values())
        for label in ['resolved', 'completed_unresolved', 'baseline_timing_sensitive_unresolved']},
    'instances': ledger}, indent=2))
print(json.dumps({'ledger': str(out), 'new_resolved_ids': new, 'cumulative': 182 + len(new)}, indent=2))
