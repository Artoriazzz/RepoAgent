import importlib.util
from pathlib import Path

import pytest


def load_runner(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("global_only20_runner", scripts / "run_global_only20_wsl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selection_uses_global_ranking_and_removes_repository_memory(monkeypatch):
    runner = load_runner(monkeypatch)
    selection = {"instances": [
        {"id": case, "saved_memories": {
            "global_experience": {"filename": filename},
            "repository_experience": {"filename": "private-repository-memory.md"},
        }} for case, filename in (("case-a", "global-a.md"), ("case-b", "global-b.md"))
    ]}
    # 即使排名第一来自另一道题，也必须按检索结果选择，而不是按当前题号取文件。
    rankings = [
        {"instance_id": case, "query_status": "success", "rankings": {
            "global_experience": [{"rank": 1, "filename": "global-b.md", "cosine_similarity": 0.8}]}}
        for case in ("case-a", "case-b")
    ]
    result = runner.global_only_selection(selection, rankings)
    assert all(set(row["saved_memories"]) == {"global_experience"} for row in result["instances"])
    assert result["instances"][0]["global_memory_source_case_id"] == "case-b"
    assert result["instances"][0]["saved_memories"]["global_experience"]["filename"] == "global-b.md"
    assert "repository_experience" in selection["instances"][0]["saved_memories"]


def test_global_only_injection_preserves_frozen_bytes_and_passes_audit(tmp_path, monkeypatch):
    runner = load_runner(monkeypatch)
    from repo_agent.core.memory import MemoryStore
    from pinned_memory_agent import inject_pinned_experiences
    from run_pinned20_wsl import audit_case

    baseline = tmp_path / "baseline"
    original_store = MemoryStore.for_scope("sample/repo", root=baseline / "memory/source-case")
    filename = original_store.save_memory("example", "description", "global_experience", "ORIGINAL_GLOBAL_RULE")
    original_root = baseline / "memory/source-case"
    original_file = original_store.global_memory_dir / filename
    row = {"id": "target-case", "repo": "sample/repo", "global_memory_source_case_id": "source-case",
           "saved_memories": {"global_experience": {"filename": filename,
                                                     "arguments": {"content": "ORIGINAL_GLOBAL_RULE"}}}}
    config = {"memory_sha256": {"source-case": runner.memory_hashes(original_root)}}
    output = tmp_path / "output"
    manifest = runner.copy_original_globals({"instances": [row]}, baseline, config, output)
    assert manifest["instances"]["target-case"] == [filename]
    store = MemoryStore.for_scope("sample/repo", root=output / "memory/target-case")
    assert (store.global_memory_dir / filename).read_bytes() == original_file.read_bytes()
    assert len(store.list_memories()) == 1
    text, memories = inject_pinned_experiences(task="PUBLIC_TASK", memory_store=store, filenames=[filename])
    path = output / "cases/target-case/target-case/pass-1/memory_injection.json"
    path.parent.mkdir(parents=True)
    runner.save_json(path, {"memories": [{"filename": filename,
                                        "injected_content_sha256": runner.digest(original_file)}]})
    path.with_name("pinned_coordinator_input.md").write_text(text)
    audit = audit_case(output / "cases/target-case", row, runner.memory_hashes(output / "memory/target-case"),
                       output / "memory/target-case")
    assert audit["verified"] and audit["injected_memory_count"] == 1


def test_query_failure_cannot_silently_become_successful_retrieval(monkeypatch):
    runner = load_runner(monkeypatch)
    selection = {"instances": [{"id": "case-a", "saved_memories": {
        "global_experience": {"filename": "global-a.md"}}}]}
    with pytest.raises(ValueError, match="Query extraction"):
        runner.global_only_selection(selection, [{"instance_id": "case-a", "query_status": "fallback_api_error"}])
