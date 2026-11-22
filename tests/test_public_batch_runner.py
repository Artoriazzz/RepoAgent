import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


spec = importlib.util.spec_from_file_location(
    'public_batch_runner', Path(__file__).parents[1] / 'scripts/run_public_tests_best35_wsl.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_all_cohort_repositories_have_preflight_imports():
    assert set(runner.IMPORTS) == {
        'django/django', 'matplotlib/matplotlib', 'mwaskom/seaborn', 'pallets/flask',
        'psf/requests', 'pydata/xarray', 'pylint-dev/pylint', 'pytest-dev/pytest',
        'scikit-learn/scikit-learn', 'sphinx-doc/sphinx', 'sympy/sympy',
    }


@pytest.mark.parametrize('output,kind,expected', [
    ('RateLimitError: {"error":{"code":"1113","message":"quota"}}', 'RateLimitError', True),
    ("RateLimitError: {'error': {'code': '1113', 'message': 'quota'}}", 'RateLimitError', True),
    ('RateLimitError: 余额不足或无可用资源包', 'RateLimitError', True),
    ('RateLimitError: {"error":{"code":"1302","message":"too many requests"}}', 'RateLimitError', False),
    ('Request timed out', 'APITimeoutError', False),
])
def test_only_balance_errors_stop_pending_tasks(tmp_path, output, kind, expected):
    path = tmp_path / 'agent_stdout.txt'
    path.write_text(output)
    summary = {'api_errors': [{'type': kind, 'log_path': str(path)}]}
    assert runner.credit_exhausted(summary) is expected


def test_successful_summary_is_not_a_credit_error():
    assert runner.credit_exhausted({'status': 'agent_ok'}) is False


@pytest.mark.parametrize('repo,version,changed_file,allowed', [
    ('sphinx-doc/sphinx', '3.1', 'setup.py', True),
    ('sphinx-doc/sphinx', '3.1', 'tox.ini', True),
    ('sphinx-doc/sphinx', '3.1', 'source.py', False),
    ('sphinx-doc/sphinx', '3.3', 'setup.py', True),
    ('sphinx-doc/sphinx', '3.4', 'tox.ini', True),
    ('sphinx-doc/sphinx', '3.4', 'source.py', False),
    ('sphinx-doc/sphinx', '3.5', 'setup.py', False),
    ('pytest-dev/pytest', '4.6', 'setup.py', False),
])
def test_baseline_allows_only_known_official_setup_changes(tmp_path, repo, version, changed_file, allowed):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True)

    git('init', '-q')
    for name in ('setup.py', 'tox.ini', 'source.py'):
        (tmp_path / name).write_text('baseline')
    git('add', '.')
    commit = ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm']
    git(*commit, 'baseline')
    base = git('rev-parse', 'HEAD').strip()
    (tmp_path / changed_file).write_text('installation change')
    git('add', '.')
    git(*commit, 'setup')
    script = runner.baseline_check_script({'repo': repo, 'version': version, 'base_commit': base}, 'pass')
    result = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, capture_output=True, text=True)
    assert (result.returncode == 0) is allowed
    if allowed:
        assert json.loads(result.stdout)['setup_changes'] == [changed_file]


def test_legacy_sklearn_environment_defines_optional_qt_variable_before_activation():
    dockerfile = 'FROM base\nRUN /root/setup_env.sh\n'
    row = {'repo': 'scikit-learn/scikit-learn', 'version': '1.3'}
    assert runner.environment_dockerfile(row, dockerfile).endswith(
        'ENV QT_XCB_GL_INTEGRATION=none\nRUN /root/setup_env.sh\n')
    assert 'QT_XCB_GL_INTEGRATION' not in runner.environment_dockerfile({**row, 'version': '1.1'}, dockerfile)
