"""合并基础设施补跑，导出逐题公开覆盖证据与独立评分，不调用模型。"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text())


def final_patch(summary_path, summary=None):
    summary = read(summary_path) if summary is None else summary
    prediction_path = summary_path.parent.parent / 'predictions.json'
    instance = summary_path.parent.name
    prediction = next(row for row in read(prediction_path) if row['instance_id'] == instance)
    patch = summary['model_patch']
    if patch != prediction['model_patch']:
        raise ValueError(f'Final summary/prediction patch mismatch: {instance}')
    return patch


def select_case(base_run, retry_run, instance, progress):
    relative = Path('cases') / instance / 'results' / instance / 'summary.json'
    original = base_run / relative
    original_status = read(original).get('status') if original.is_file() else None
    retryable = original_status == 'api_error' or (
        not original.is_file() and progress.get('status') in {
            'environment_error', 'runner_error', 'waiting_for_api_credit',
        }
    )
    if retry_run and retryable:
        retry = retry_run / relative
        if retry.is_file():
            return retry
    return original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-run', type=Path, required=True)
    parser.add_argument('--retry-run', type=Path)
    parser.add_argument('--reevaluation-run', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    selected = read(args.base_run / 'experiment.json')['instances']
    progress_path = args.base_run / 'progress.json'
    progress = read(progress_path)['instances'] if progress_path.exists() else {}
    overrides = {}
    if args.reevaluation_run:
        overrides = {row['instance_id']: row for row in read(args.reevaluation_run / 'summary.json')['cases']}
    results = []
    for item in selected:
        state = progress.get(item['id'], {})
        summary_path = select_case(args.base_run, args.retry_run, item['id'], state)
        case = summary_path.parent
        has_summary = summary_path.is_file()
        summary = read(summary_path) if has_summary else {'status': state.get('status', 'missing_summary')}
        patch = final_patch(summary_path, summary) if has_summary else None
        passes = summary.get('passes', [])
        stage = passes[-1] if passes else {}
        directory = case / 'pass-1'
        public_file = directory / 'public_validation.json'
        public = read(public_file) if public_file.exists() else {}
        plan = public.get('plan', {})
        tokens = Counter()
        for path in directory.rglob('session.json'):
            session = read(path)
            for key in ('total_tokens', 'total_prompt_tokens', 'total_cached_tokens', 'total_completion_tokens'):
                tokens[key] += session.get(key, 0)
        attribution = stage.get('stage_attribution') or {}
        tests = (attribution.get('harness_attribution') or {}).get('tests', {})
        resolved = summary.get('resolved')
        override = overrides.get(item['id'])
        if override:
            resolved = override['resolved']
            statuses = override['tests_status']
            tests = {f'{prefix}_{outcome}': statuses[group][state]
                     for prefix, group in [('fail_to_pass', 'FAIL_TO_PASS'), ('pass_to_pass', 'PASS_TO_PASS')]
                     for outcome, state in [('passed', 'success'), ('failed', 'failure')]}
        final = stage.get('public_validation') or {}
        detail = {'instance_id': item['id'], 'repo': item['repo'],
                  'summary_path': str(summary_path) if has_summary else None,
                  'patch_empty': not patch.strip() if patch is not None else None,
                  'status': summary.get('status'), 'resolved': resolved,
                  'original_resolved': summary.get('resolved'),
                  'reevaluation_report': override.get('report_path') if override else None,
                  'initial_public': (stage.get('initial_public_validation') or {}).get('status'),
                  'final_public': final.get('status'),
                  'false_pass': final.get('status') == 'passed' and not resolved
                      and summary.get('status') == 'agent_ok',
                  'false_rejection': final.get('status') == 'failed' and bool(resolved),
                  'tests': tests, 'tokens': dict(tokens),
                  'public_cases': [{key: row.get(key) for key in
                      ('id', 'kind', 'command', 'expected_behavior', 'basis')}
                      for row in plan.get('cases', [])],
                  'unknowns': plan.get('unknowns', []),
                  'public_file': str(public_file), 'worker': public.get('worker'),
                  'public_issue': (directory / 'task.md').read_text() if (directory / 'task.md').exists() else None,
                  'patch': patch, 'patch_sha256': hashlib.sha256(patch.encode()).hexdigest() if patch is not None else None,
                  'initial_patch_path': str(directory / 'model.patch'),
                  'official_failure_excerpt': None if override else (stage.get('harness') or {}).get('failure_excerpt'),
                  'prior_failure_excerpt': (stage.get('harness') or {}).get('failure_excerpt') if override else None,
                  'caution': 'Official feedback is for offline audit only; never inject into test design or repair.'}
        (args.output / f'{item["id"]}.json').write_text(json.dumps(detail, ensure_ascii=False, indent=2))
        results.append({key: detail[key] for key in ('instance_id', 'repo', 'status', 'resolved',
                       'initial_public', 'final_public', 'false_pass', 'false_rejection', 'tests', 'tokens',
                       'summary_path', 'patch_empty')})
    counts, tokens = Counter(), Counter()
    for row in results:
        counts.update(row['tests'])
        tokens.update(row['tokens'])
    report = {'base_run': str(args.base_run), 'retry_run': str(args.retry_run) if args.retry_run else None,
              'resolved': sum(bool(x['resolved']) for x in results), 'total': len(results),
              'missing_summaries': sum(x['summary_path'] is None for x in results),
              'empty_patches': sum(x['patch_empty'] is True for x in results),
              'reevaluation_run': str(args.reevaluation_run) if args.reevaluation_run else None,
              'false_pass': sum(x['false_pass'] for x in results),
              'false_rejection': sum(x['false_rejection'] for x in results),
              'statuses': dict(Counter(x['status'] for x in results)),
              'tests': dict(counts), 'tokens': dict(tokens), 'cases': results}
    (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != 'cases'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
