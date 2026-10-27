#!/usr/bin/env python3
"""诊断入口：跳过检索，把清单指定的新经验注入唯一一次协调任务。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from repo_agent.core.experience import augment_coordinator_with_experiences
from repo_agent.core.memory import MemoryStore, MemoryType, RecalledMemory


def inject_pinned_experiences(*, task: str, memory_store: MemoryStore, filenames: list[str]):
    """只读取显式文件；保留正常格式化流程，但完全不执行关键词或 BM25 排名。"""
    if not filenames or len(filenames) != len(set(filenames)):
        raise ValueError("Specify a nonempty list of distinct memory filenames")
    headers = {header.filename: header for header in memory_store.list_memories()}
    selected = []
    for filename in filenames:
        header = headers[filename]
        if header.type not in {MemoryType.GLOBAL_EXPERIENCE, MemoryType.REPOSITORY_EXPERIENCE}:
            raise ValueError(f"Not a new experience: {filename}")
        selected.append(RecalledMemory(header=header, content=memory_store.read_memory(filename)))

    class PinnedStore:
        def recall_experiences(self, query, *, memory_type, limit):
            return [memory for memory in selected if memory.header.type == memory_type][:limit]

    return augment_coordinator_with_experiences(
        task=task, memory_store=PinnedStore(), limit_per_type=len(selected),
    )


def main() -> None:
    from repo_agent import cli
    from repo_agent.core import coordinator

    args = cli.build_parser().parse_args()
    if args.command != "run" or not args.coordinator or args.repair_mode != "benchmark":
        raise SystemExit("Pinned-memory diagnostics require run --coordinator --repair-mode benchmark")
    if args.no_repair_memory:
        raise SystemExit("Pinned-memory diagnostics cannot disable Coordinator memory")
    manifest = json.loads(Path(os.environ["REPO_AGENT_PINNED_MEMORY_MANIFEST"]).read_text())
    filenames = manifest["instances"][args.repo.name]

    def pinned_augment(*, task, memory_store, limit_per_type=2):
        augmented, memories = inject_pinned_experiences(
            task=task, memory_store=memory_store, filenames=filenames,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "pinned_coordinator_input.md").write_text(augmented, encoding="utf-8")
        audit = {
            "diagnostic_only": True,
            "selection": "explicit_filenames_without_retrieval",
            "instance_id": args.repo.name,
            "memories": [
                {"filename": memory.header.filename, "type": memory.header.type.value,
                 "provenance": memory.header.provenance,
                 "injected_content_sha256": hashlib.sha256(memory.content.encode()).hexdigest()}
                for memory in memories
            ],
            "coordinator_input_sha256": hashlib.sha256(augmented.encode()).hexdigest(),
        }
        (args.output_dir / "memory_injection.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        return augmented, memories

    # 只替换当前诊断进程的选择入口，不改生产检索器或冻结代码文件。
    coordinator.augment_coordinator_with_experiences = pinned_augment
    cli.main()


if __name__ == "__main__":
    main()
