"""从冻结的直接注入实验导出带检索字段的经验副本，不修改原实验。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export(source: Path, annotations_path: Path, output: Path) -> dict:
    selection = json.loads((source / "selection.json").read_text())
    experiment = json.loads((source / "experiment.json").read_text())
    annotations = json.loads(annotations_path.read_text())["instances"]
    cases = selection["instances"]
    ids = [case["id"] for case in cases]
    if len(ids) != 20 or len(set(ids)) != 20 or set(ids) != set(annotations):
        raise ValueError("清单和标注必须对应同一组 20 个问题")

    # 先校验所有源材料，再创建新目录；任何缺失均不生成不完整的经验集。
    prepared = []
    for case in cases:
        instance = case["id"]
        fields = annotations[instance]
        if set(fields) != {"problem_description", "usage_scenario"}:
            raise ValueError(f"检索字段不匹配: {instance}")
        if any(not isinstance(v, str) or not v.strip() or "\n" in v for v in fields.values()):
            raise ValueError(f"检索字段必须为非空单行文本: {instance}")
        candidates = [
            source / "cases" / instance / instance / "pass-1/task.md",
            source.parent / "lite-pinned20-external2-retry-glm-20261025" / "cases"
            / instance / instance / "pass-1/task.md",
            Path(case["pass_output"]) / "task.md",
        ]
        task_path = next((path for path in candidates if path.is_file()), None)
        if task_path is None:
            raise FileNotFoundError(f"缺少公开任务原文: {instance}")
        task_bytes = task_path.read_bytes()
        marker = "## Problem Statement\n"
        if marker not in task_bytes.decode():
            raise ValueError(f"无法分离公开问题: {task_path}")
        problem = task_bytes.decode().split(marker, 1)[1].strip()
        frozen = experiment["memory_sha256"][instance]
        for kind in ("global_experience", "repository_experience"):
            saved = case["saved_memories"][kind]
            matches = list((source / "memory" / instance).rglob(saved["filename"]))
            if len(matches) != 1:
                raise ValueError(f"经验文件不唯一或缺失: {instance}/{saved['filename']}")
            path = matches[0]
            raw = path.read_bytes()
            relative = str(path.relative_to(source / "memory" / instance))
            if frozen.get(relative) != sha256(raw):
                raise ValueError(f"原经验与冻结哈希不一致: {path}")
            text = raw.decode()
            if not text.startswith("---\n") or "\n---\n" not in text[4:]:
                raise ValueError(f"原经验缺少 metadata: {path}")
            header, body = text[4:].split("\n---\n", 1)
            if body.strip() != saved["arguments"]["content"].strip():
                raise ValueError(f"原经验与清单正文不一致: {path}")
            if any(line.startswith(("problem_description:", "usage_scenario:")) for line in header.splitlines()):
                raise ValueError(f"原经验已存在检索字段: {path}")
            enriched = "---\n" + header + "\n" + "\n".join(
                f"{key}: {value}" for key, value in fields.items()
            ) + "\n---\n" + body
            prepared.append((case, kind, saved, path, raw, enriched, task_path, task_bytes, problem))

    output.mkdir(parents=True, exist_ok=False)
    for name in ("memories", "original_memories", "source_tasks"):
        (output / name).mkdir()
    save_json(output / "annotations.json", json.loads(annotations_path.read_text()))
    records = []
    for case, kind, saved, path, raw, enriched, task_path, task_bytes, problem in prepared:
        filename = saved["filename"]
        (output / "original_memories" / filename).write_bytes(raw)
        target = output / "memories" / filename
        target.write_text(enriched, encoding="utf-8")
        task_copy = output / "source_tasks" / f"{case['id']}.md"
        task_copy.write_bytes(task_bytes)
        fields = annotations[case["id"]]
        records.append({
            "source_case_id": case["id"], "repository": case["repo"],
            "memory_type": kind, "memory_filename": filename,
            **fields,
            "retrieval_text": f"问题描述：{fields['problem_description']}\n使用场景：{fields['usage_scenario']}",
            "original_problem_statement": problem,
            "original_memory": saved["arguments"],
            "provenance": saved["arguments"].get("provenance", ""),
            "source_memory_path": str(path), "source_task_path": str(task_path),
            "source_curator_messages": saved["source_messages"],
            "source_memory_sha256": sha256(raw), "source_task_sha256": sha256(task_bytes),
            "enriched_memory_sha256": sha256(target.read_bytes()),
            "problem_description_source": "public_issue_only",
            "usage_scenario_source": "public_issue_and_existing_experience",
        })
    if len(records) != 40 or len({r["memory_filename"] for r in records}) != 40:
        raise ValueError("输出必须包含 40 个不同经验文件")
    save_json(output / "retrieval_records.json", {
        "schema_version": 1, "diagnostic_only": True,
        "source_experiment": str(source), "records": records,
    })
    (output / "retrieval_records.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8"
    )
    review = ["# 直接注入实验经验的检索字段", "",
              "20 个案例，40 条经验。原经验逐字保留；新增字段仅用于后续检索。",
              "problem_description 只概括公开 Issue；usage_scenario 可使用已有经验确认的适用条件。",
              "全局与仓库经验分别保留记录；同一案例的两个检索字段共享，不视为两份独立检索证据。", ""]
    for case in cases:
        fields = annotations[case["id"]]
        review.extend([f"## {case['id']}", "", f"仓库：{case['repo']}", "",
                       f"**problem_description**：{fields['problem_description']}", "",
                       f"**usage_scenario**：{fields['usage_scenario']}", ""])
        for kind in ("global_experience", "repository_experience"):
            filename = case["saved_memories"][kind]["filename"]
            review.append(f"- [{filename}](memories/{filename})")
        review.append("")
    (output / "README.md").write_text("\n".join(review), encoding="utf-8")

    # 导出后再次验证源文件未变，同时确认备份字节和正文均原样保留。
    for case, kind, saved, path, raw, enriched, task_path, task_bytes, problem in prepared:
        if path.read_bytes() != raw or task_path.read_bytes() != task_bytes:
            raise ValueError(f"导出过程中来源发生变化: {path}")
        if (output / "original_memories" / saved["filename"]).read_bytes() != raw:
            raise ValueError("原经验备份不一致")
        new_text = (output / "memories" / saved["filename"]).read_text()
        if new_text[4:].split("\n---\n", 1)[1] != raw.decode()[4:].split("\n---\n", 1)[1]:
            raise ValueError("经验正文被修改")
    summary = {
        "cases": len(cases), "memories": len(records),
        "global_experiences": sum(r["memory_type"] == "global_experience" for r in records),
        "repository_experiences": sum(r["memory_type"] == "repository_experience" for r in records),
        "original_files_unchanged": True, "original_bodies_preserved": True,
        "source_selection_sha256": sha256((source / "selection.json").read_bytes()),
        "annotations_sha256": sha256(annotations_path.read_bytes()),
        "retrieval_records_sha256": sha256((output / "retrieval_records.json").read_bytes()),
        "embedding_computed": False,
    }
    save_json(output / "summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.source, args.annotations, args.output), ensure_ascii=False, indent=2))
