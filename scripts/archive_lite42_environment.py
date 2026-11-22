"""Preserve the environment audit without copying provider credentials."""
import difflib
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path('/home/artoria/workspace/SWE-bench')
RUNS = ROOT / 'agent_work/runs'
OUT = RUNS / 'lite42-environment-archive-20261122'
OUT.mkdir(parents=True, exist_ok=True)
files = [
    RUNS / 'lite42-environment-ledger-20261122.json',
    RUNS / 'lite-cumulative-194-audit-20261122/resolved-index.json',
    RUNS / 'lite42-matplotlib23913-timing-audit/results.json',
    ROOT / 'swebench/harness/log_parsers/python.py',
    ROOT / 'swebench/harness/log_parsers/python.py.before-repoagent-verbose-20261122',
    Path('/tmp/repoagent-audit-lite129-result.json'),
    Path('/tmp/repoagent-lite-remaining119-selection.json'),
]
manifest = []
for source in files:
    assert source.is_file(), source
    target = OUT / source.name
    shutil.copy2(source, target)
    manifest.append({'source':str(source), 'archived':str(target),
                     'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
ledger = json.loads((OUT / 'lite42-environment-ledger-20261122.json').read_text())
reports = []
for instance_id, row in ledger['instances'].items():
    report = Path(row['result_path'])
    log = Path(row['test_output_path'])
    assert report.is_file() and log.is_file(), instance_id
    reports.append({'instance_id':instance_id, 'report':str(report),
                    'report_sha256':hashlib.sha256(report.read_bytes()).hexdigest(),
                    'log':str(log), 'log_sha256':hashlib.sha256(log.read_bytes()).hexdigest()})
(OUT / 'report-hashes.json').write_text(json.dumps(reports,indent=2))
before = (OUT / 'python.py.before-repoagent-verbose-20261122').read_text().splitlines(True)
after = (OUT / 'python.py').read_text().splitlines(True)
(OUT / 'harness-parser.patch').write_text(''.join(difflib.unified_diff(before,after,
    fromfile='python.py.before',tofile='python.py')))
(OUT / 'manifest.json').write_text(json.dumps({'files':manifest,
    'scope':42, 'reports_verified':len(reports),
    'credentials_copied':False, 'remaining_decision':'Keep timing-sensitive 23913 unresolved or investigate further'},indent=2))
print(json.dumps({'archive':str(OUT), 'reports_verified':len(reports)}))
