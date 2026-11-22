"""同一组已有 patch 的两套冻结用例对照；只在容器执行，不调用模型。"""

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from audit_public_tests_wsl import final_patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evaluation-source', type=Path, required=True)
    parser.add_argument('--reference-audit', type=Path, required=True)
    parser.add_argument('--reference-label', default='v12')
    parser.add_argument('--candidate-run', type=Path, required=True)
    parser.add_argument('--candidate-label', default='v22')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.evaluation_source))
    from repo_agent.core.public_validation import evaluate_public_plan
    from repo_agent.core.verification import test_overlay_digest

    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(__file__, args.output / 'orchestration.py')
    (args.output / 'experiment.json').write_text(json.dumps({
        'reference_audit': str(args.reference_audit), 'reference_label': args.reference_label,
        'candidate_run': str(args.candidate_run), 'candidate_label': args.candidate_label,
        'evaluation_source': str(args.evaluation_source), 'model_calls': False,
        'patch_source': 'summary.model_patch verified against root predictions.json',
    }, indent=2))
    reference = {x['instance_id']: x for x in json.loads(args.reference_audit.read_text())['cases']}
    selected = json.loads((args.candidate_run / 'experiment.json').read_text())['instances']
    rows = []
    for item in selected:
        instance = item['id']
        saved = reference[instance]
        reference_path = Path(saved['summary_path'])
        summary = json.loads(reference_path.read_text())
        patch_text = final_patch(reference_path, summary)
        if not patch_text.strip():
            rows.append({'instance': instance, 'status': 'skipped_empty_reference_patch'})
            continue
        patch = args.output / f'{instance}.patch'
        patch.write_text(patch_text)
        candidate = args.candidate_run / 'cases' / instance / 'results' / instance
        for design, directory in [(args.reference_label, reference_path.parent), (args.candidate_label, candidate)]:
            path = directory / 'pass-1/public_validation.json'
            if not path.exists():
                rows.append({'instance': instance, 'design': design, 'status': 'missing_design'})
                continue
            artifact = json.loads(path.read_text())
            overlay = artifact.get('test_files_dir')
            digest = test_overlay_digest(Path(overlay) if overlay else None)
            case_dir = args.output / instance / design
            case_dir.mkdir(parents=True)
            executions = []

            def execute(command, patched=True):
                script = f"git diff --exit-code {shlex.quote(summary['base_commit'])} -- . && "
                argv = ['docker', 'run', '--rm', '--network', 'none', '-w', '/testbed']
                if patched:
                    argv += ['-v', f'{patch}:/candidate.patch:ro']
                    script += 'git apply --whitespace=nowarn /candidate.patch && '
                if overlay:
                    argv += ['-v', f'{overlay}:/public-tests:ro']
                    script += 'cp -R /public-tests/. /testbed/ && '
                argv += [f'sweb.eval.x86_64.{instance}:latest', 'bash', '-lc', script + command]
                try:
                    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, timeout=240)
                    code, output = result.returncode, result.stdout
                except subprocess.TimeoutExpired:
                    code, output = None, 'External comparison exceeded 240 seconds'
                index = len(executions) + 1
                (case_dir / f'execution-{index}.log').write_text(output)
                execution = {'command': command, 'exit_code': code, 'status': 'passed' if code == 0 else 'failed',
                             'evidence': output[-6000:], 'runtime': 'swebench_docker',
                             'test_files_sha256': digest}
                executions.append({'patched': patched, **execution})
                return execution

            # 同一清理后镜像重新跑基线，避免沿用受污染环境的基线分类。
            baseline = {case['id']: execute(case['command'], patched=False)
                        for case in artifact['plan'].get('cases', [])}
            validation = evaluate_public_plan(artifact['plan'], baseline, execute)
            row = {'instance': instance, 'design': design, 'official_resolved': saved['resolved'],
                   'patch_sha256': hashlib.sha256(patch_text.encode()).hexdigest(),
                   'reference_summary': str(reference_path), 'status': validation['status'],
                   'baseline': baseline, 'validation': validation}
            (case_dir / 'executions.json').write_text(json.dumps(executions, ensure_ascii=False, indent=2))
            rows.append(row)
            (args.output / 'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print({k: v for k, v in row.items() if k not in {'baseline', 'validation'}}, flush=True)
    (args.output / 'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    (args.output / 'finished.txt').write_text('done\n')


if __name__ == '__main__':
    main()
