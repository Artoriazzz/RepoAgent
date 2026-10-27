import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def test_resume_reuses_initial_patch_without_reusing_failed_evaluation(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/resume_lite_harness_errors_wsl.py"
    spec = importlib.util.spec_from_file_location("harness_continuation", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    batch = SimpleNamespace(restore_worktree_patch=Mock())
    previous = {
        "status": "agent_ok", "agent_exit_code": 0, "seconds": 50,
        "stage_attribution": {"token_usage": {"total": 100}, "stages": {"implementation": {"report": {}}}},
        "harness": {"error": True}, "harness_feedback": {"feedback_eligible": False},
    }
    saved_patch = "diff --git a/app.py b/app.py\n"
    repo = tmp_path / "repo"
    output = tmp_path / "pass-1"
    result = module.saved_generation(batch, previous, saved_patch)(
        {}, repo, output, "public issue with resumed repo path", None, {}, 0)
    batch.restore_worktree_patch.assert_called_once_with(repo, saved_patch)
    assert "harness" not in result and "harness_feedback" not in result
    assert previous["harness"]["error"] is True
    assert result["initial_generation_reused"] is True and result["seconds"] == 0
    assert result["stage_attribution"] == previous["stage_attribution"]
    assert (output / "model.patch").read_text() == saved_patch
    assert (output / "task.md").read_text() == "public issue with resumed repo path"
