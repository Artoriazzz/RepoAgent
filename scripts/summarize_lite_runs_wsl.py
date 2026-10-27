"""按明确的续跑顺序汇总 Lite 300 题，保留逐题来源，避免按成败挑选记录。"""
import argparse
from collections import Counter
import json
from pathlib import Path


ROOT = Path('/home/artoria/workspace/SWE-bench')
WORK = ROOT / 'agent_work'
RUN_NAMES = [
    'lite-first20-expbuild-glm-exactedit-20261020',
    'lite-first20-envfix-cont-glm-20261020',
    'lite20to39-expbuild-glm-exactedit-20261021',
    'lite40to109-api43-retry-glm-20261022',
    'external3-continuation-glm-20261023',
    'lite200to299-env79-plus300-glm-20261024',
    'lite-external2-stream-env-retry-v2-glm-20261024',
]


def test_counts(harness):
    status = harness.get('instance_report', {}).get('tests_status', {})
    return {kind: {key: len(values.get(key, [])) for key in ('success', 'failure')}
            for kind, values in status.items() if kind in ('FAIL_TO_PASS', 'PASS_TO_PASS')}


def aggregate(rows):
    return {'total': len(rows), 'initial_resolved': sum(r['initial_resolved'] for r in rows),
            'final_resolved': sum(r['final_resolved'] for r in rows),
            'unresolved': sum(not r['final_resolved'] for r in rows),
            'statuses': dict(Counter(r['status'] for r in rows)),
            'harness_errors': sum(r['harness_error'] for r in rows),
            'api_error_cases': sum(bool(r['api_errors']) for r in rows),
            'invalid_patch': sum(r['status'] == 'invalid_patch' for r in rows)}


def main():
    import pandas as pd
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=WORK / 'reports/lite-full-summary-20261024')
    args = parser.parse_args()
    dataset = pd.read_parquet(ROOT / 'test-00000-of-00001.parquet').to_dict('records')
    order = {r['instance_id']: i for i, r in enumerate(dataset)}
    latest = {}
    for name in RUN_NAMES:
        source = WORK / 'runs' / name / 'results.json'
        for row in json.loads(source.read_text()):
            initial = (row.get('passes') or [{}])[0]
            harness = initial.get('harness', {})
            first = initial.get('initial_harness', harness)
            latest[row['instance_id']] = {
                'instance_id': row['instance_id'], 'dataset_index': order[row['instance_id']],
                'repo': next(r['repo'] for r in dataset if r['instance_id'] == row['instance_id']),
                'status': row.get('status'), 'source': str(source), 'output_dir': row.get('output_dir'),
                'initial_resolved': bool(first.get('resolved')), 'final_resolved': bool(row.get('resolved')),
                'harness_error': bool(first.get('error') or harness.get('error')),
                'api_errors': row.get('api_errors', []),
                'initial_tests': test_counts(first), 'final_tests': test_counts(harness),
                'initial_report_path': first.get('report_path'), 'final_report_path': harness.get('report_path'),
                'patch_validation_reasons': initial.get('patch_validation', {}).get('reasons', [])}
    # 两条补评结果没有统一的 passes 结构，以各自实际 Harness 报告补齐。
    for suffix, instance in [('12983-20261022', 'django__django-12983'),
                             ('15819-proxyfix-20261022', 'django__django-15819')]:
        run_id = 'lite-first-round-supplement-' + suffix
        source = ROOT / 'logs/run_evaluation' / run_id / 'repoagent-glm-5.3-flash' / instance / 'report.json'
        report = json.loads(source.read_text())[instance]
        previous = latest[instance]
        previous.update(status='evaluated_saved_initial_patch', source=str(source),
                        initial_resolved=bool(report['resolved']), final_resolved=bool(report['resolved']),
                        harness_error=False, api_errors=[], initial_report_path=str(source), final_report_path=str(source),
                        initial_tests=test_counts({'instance_report': report}),
                        final_tests=test_counts({'instance_report': report}), patch_validation_reasons=[])
    if set(latest) != set(order):
        raise RuntimeError(f'Dataset mismatch: missing={sorted(set(order)-set(latest))}, extra={sorted(set(latest)-set(order))}')
    rows = sorted(latest.values(), key=lambda row: row['dataset_index'])
    summary = {'dataset': 'SWE-bench Lite test', 'model': 'glm-5.3-flash',
               'diagnostic_only': True, 'single_frozen_run': False,
               'selection': 'Explicit chronological continuations; later listed records replace earlier ones regardless of outcome.',
               'sources': RUN_NAMES, **aggregate(rows),
               'ranges': {f'{start}-{stop-1}': aggregate(rows[start:stop])
                          for start, stop in [(0, 110), (110, 200), (200, 300)]},
               'repositories': {repo: aggregate([r for r in rows if r['repo'] == repo])
                                for repo in sorted({r['repo'] for r in rows})}}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for filename, value in [('summary.json', summary), ('instances.json', rows),
                            ('unresolved.json', [r for r in rows if not r['final_resolved']])]:
        (args.output_dir / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
