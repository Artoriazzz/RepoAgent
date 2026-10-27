import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

from repo_agent.core.memory import MemoryStore, MemoryType


def load_script():
    path = Path(__file__).resolve().parents[1] / "scripts/pinned_memory_agent.py"
    spec = importlib.util.spec_from_file_location("pinned_memory_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_explicit_memories_bypass_retrieval_and_exclude_unselected(tmp_path):
    store = MemoryStore(tmp_path / "repo", tmp_path / "global")
    global_file = store.save_memory("global", "chosen", "global_experience", "GLOBAL_RULE")
    repo_file = store.save_memory("repository", "chosen", "repository_experience", "REPOSITORY_RULE")
    store.save_memory("irrelevant", "unselected", "global_experience", "UNRELATED_RULE")
    store._recall_bm25 = Mock(side_effect=AssertionError("Must not rank"))
    store.recall_experiences = Mock(side_effect=AssertionError("Must not retrieve"))
    text, memories = load_script().inject_pinned_experiences(
        task="PUBLIC_TASK", memory_store=store, filenames=[global_file, repo_file],
    )
    assert "GLOBAL_RULE" in text and "REPOSITORY_RULE" in text
    assert "UNRELATED_RULE" not in text and text.endswith("PUBLIC_TASK")
    assert [m.header.type for m in memories] == [MemoryType.GLOBAL_EXPERIENCE, MemoryType.REPOSITORY_EXPERIENCE]
    store.recall_experiences.assert_not_called()
    store._recall_bm25.assert_not_called()


def test_explicit_memories_reject_legacy_and_missing_files(tmp_path):
    store = MemoryStore(tmp_path)
    legacy = store.save_memory("old", "legacy", "repair_recipe", "OLD")
    inject = load_script().inject_pinned_experiences
    with pytest.raises(ValueError, match="Not a new experience"):
        inject(task="task", memory_store=store, filenames=[legacy])
    with pytest.raises(KeyError):
        inject(task="task", memory_store=store, filenames=["missing.md"])
    with pytest.raises(ValueError, match="nonempty"):
        inject(task="task", memory_store=store, filenames=[])
