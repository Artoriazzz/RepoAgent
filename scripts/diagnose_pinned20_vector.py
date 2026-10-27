"""对冻结的 20 题做查询提取和向量召回诊断，不启动任何修复角色。"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--query-module-path", type=Path, required=True)
    parser.add_argument("--llm-module-path", type=Path, required=True)
    parser.add_argument("--previous-result", type=Path, required=True)
    parser.add_argument("--embedding-python", type=Path, required=True)
    parser.add_argument("--embedding-model-path", type=Path, required=True)
    args = parser.parse_args()
    for line in args.env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip("'\"")
    llm_module = load_module("repo_agent.core.llm", args.llm_module_path)
    query_module = load_module("pinned20_memory_query", args.query_module_path)
    from repo_agent.core.experience import augment_coordinator_with_experiences
    from repo_agent.core.memory import MemoryStore, MemoryType, RecalledMemory
    from repo_agent.core.semantic_evidence import extract_public_problem_statement

    source = args.dataset / "retrieval_records.json"
    records = json.loads(source.read_text())["records"]
    ids = list(dict.fromkeys(r["source_case_id"] for r in records))
    if len(ids) != 20 or len(records) != 40:
        raise ValueError("Expected the frozen 20-case, 40-memory dataset")
    args.output.mkdir(parents=True, exist_ok=False)
    original_hashes = {r["memory_filename"]: digest((args.dataset / "memories" / r["memory_filename"]).read_bytes())
                       for r in records}
    stores = {}
    for record in records:
        repo = record["repository"]
        store = stores.setdefault(repo, MemoryStore.for_scope(repo, root=args.output / "memory"))
        directory = store.global_memory_dir if record["memory_type"] == "global_experience" else store.memory_dir
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.dataset / "memories" / record["memory_filename"], directory / record["memory_filename"])
    for store in stores.values():
        store.update_index()

    tasks = {instance: (args.dataset / "source_tasks" / f"{instance}.md").read_text() for instance in ids}
    public_issues = {instance: extract_public_problem_statement(task) for instance, task in tasks.items()}
    model = os.getenv("REPO_AGENT_MEMORY_QUERY_MODEL") or os.getenv("OPENAI_MODEL")
    agent = query_module.MemoryQueryAgent(llm_module.ChatLLM(
        model=model,
        api_key=os.getenv("REPO_AGENT_MEMORY_QUERY_API_KEY") or os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("REPO_AGENT_MEMORY_QUERY_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
        enable_thinking=False, reasoning_effort="low", max_output_tokens=4096,
        output_limit_recovery=False,
    ))
    previous_report = json.loads((args.previous_result / "retrieval_report.json").read_text())
    previous = previous_report["instance_id"]
    previous_query = args.previous_result / "memory-query/result.json"
    previous_request = json.loads(previous_query.with_name("request.json").read_text())
    if json.loads(previous_request["messages"][1]["content"])["issue_body"] != public_issues[previous]:
        raise ValueError("Previous query does not match the frozen public Issue")

    queries = {}
    reused = set()
    previous_dir = args.output / "queries" / previous
    previous_dir.mkdir(parents=True)
    shutil.copy2(previous_query, previous_dir / "result.json")
    shutil.copy2(previous_query.with_name("request.json"), previous_dir / "request.json")
    queries[previous] = json.loads((previous_dir / "result.json").read_text())
    reused.add(previous)
    save(args.output / "progress.json", {"phase": "query_extraction", "completed": 1, "total": 20})

    def extract(instance):
        directory = args.output / "queries" / instance
        agent.extract(issue_body=public_issues[instance], output_dir=directory)
        return json.loads((directory / "result.json").read_text())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(extract, instance): instance for instance in ids if instance not in reused}
        for future in as_completed(futures):
            instance = futures[future]
            queries[instance] = future.result()
            save(args.output / "progress.json", {"phase": "query_extraction", "completed": len(queries),
                                                  "total": 20, "last_instance": instance,
                                                  "last_status": queries[instance]["status"]})
            print(f"query {len(queries)}/20 {instance}: {queries[instance]['status']}", flush=True)

    save(args.output / "progress.json", {"phase": "embedding", "completed": 20, "total": 20})
    encoded = json.loads((args.previous_result / "embeddings.json").read_text())
    previous_inputs = {item["id"]: item for item in json.loads((args.previous_result / "embedding_input.json").read_text())}
    if encoded["model_path"] != str(args.embedding_model_path):
        raise ValueError("Embedding model revision differs from cached vectors")
    record_keys = {}
    for record in records:
        text = f"问题描述：{record['problem_description']}\n使用场景：{record['usage_scenario']}"
        key = digest(text.encode())
        if previous_inputs.get(key) != {"id": key, "text": text, "is_query": False}:
            raise ValueError("Historical retrieval fields differ from cached vectors")
        record_keys[record["memory_filename"]] = key
    if queries[previous]["retrieval_text"] != previous_inputs["query"]["text"]:
        raise ValueError("Previous query differs from cached vector")
    inputs = [{"id": f"query::{instance}", "text": queries[instance]["retrieval_text"], "is_query": True}
              for instance in ids if instance not in reused]
    save(args.output / "embedding_input.json", inputs)
    subprocess.run([
        str(args.embedding_python), str(Path(__file__).with_name("embed_retrieval_texts.py")),
        "--model-path", str(args.embedding_model_path),
        "--input", str(args.output / "embedding_input.json"),
        "--output", str(args.output / "query_embeddings.json"),
    ], check=True)
    new_encoded = json.loads((args.output / "query_embeddings.json").read_text())
    vectors = {**encoded["vectors"], **new_encoded["vectors"]}
    rows = {r["memory_filename"]: r for r in records}
    results = []
    for instance in ids:
        expected = [r for r in records if r["source_case_id"] == instance]
        repo = expected[0]["repository"]
        store = stores[repo]
        query_key = "query" if instance in reused else f"query::{instance}"
        scores = {
            h.filename: max(sum(a * b for a, b in zip(q, d)) for q in vectors[query_key]
                            for d in vectors[record_keys[h.filename]])
            for h in store.list_memories()
        }
        rankings = {}
        for kind in (MemoryType.GLOBAL_EXPERIENCE, MemoryType.REPOSITORY_EXPERIENCE):
            headers = [h for h in store.list_memories() if h.type == kind]
            headers.sort(key=lambda h: (-scores[h.filename], h.filename))
            rankings[kind.value] = [RecalledMemory(header=h, content=store.read_memory(h.filename)) for h in headers]

        class RankedStore:
            def recall_experiences(self, query, *, memory_type, limit):
                return rankings[memory_type.value][:limit]

        text, selected = augment_coordinator_with_experiences(
            task=tasks[instance], memory_store=RankedStore(), limit_per_type=1,
        )
        directory = args.output / "cases" / instance
        directory.mkdir(parents=True)
        (directory / "coordinator_input.md").write_text(text)
        (directory / "injected_experiences.md").write_text(text[:-len(tasks[instance])])
        target_results = {}
        for target in expected:
            ranked = rankings[target["memory_type"]]
            rank = next(i for i, memory in enumerate(ranked, 1) if memory.header.filename == target["memory_filename"])
            target_results[target["memory_type"]] = {
                "expected_filename": target["memory_filename"], "rank": rank,
                "hit_at_1": rank == 1, "cosine_similarity": scores[target["memory_filename"]],
                "selected_filename": ranked[0].header.filename,
                "selected_source_case": rows[ranked[0].header.filename]["source_case_id"],
                "selected_score": scores[ranked[0].header.filename],
                "full_target_body_injected": target["original_memory"]["content"].strip() in text,
                "candidate_count": len(ranked),
            }
        record = {
            "instance_id": instance, "repository": repo, "query_status": queries[instance]["status"],
            "query_reused": instance in reused, "targets": target_results,
            "both_hit_at_1": all(r["hit_at_1"] for r in target_results.values()),
            "injection_clipped": "[... clipped " in text,
            "rankings": {kind: [{"rank": i, "filename": m.header.filename,
                                  "source_case_id": rows[m.header.filename]["source_case_id"],
                                  "cosine_similarity": scores[m.header.filename]}
                                 for i, m in enumerate(memories, 1)] for kind, memories in rankings.items()},
            "selected_memories": [m.header.filename for m in selected],
        }
        save(directory / "retrieval_report.json", record)
        results.append(record)
    unchanged = all(digest((args.dataset / "memories" / name).read_bytes()) == value
                    for name, value in original_hashes.items())
    if not unchanged:
        raise ValueError("Frozen memory dataset changed during diagnostics")
    summary = {
        "cases": 20, "query_extraction_successes": sum(q["status"] == "success" for q in queries.values()),
        "global_hit_at_1": sum(r["targets"]["global_experience"]["hit_at_1"] for r in results),
        "repository_hit_at_1": sum(r["targets"]["repository_experience"]["hit_at_1"] for r in results),
        "both_hit_at_1": sum(r["both_hit_at_1"] for r in results),
        "both_hit_with_successful_extraction": sum(r["both_hit_at_1"] and r["query_status"] == "success" for r in results),
        "global_hit_at_3": sum(r["targets"]["global_experience"]["rank"] <= 3 for r in results),
        "global_mrr": sum(1 / r["targets"]["global_experience"]["rank"] for r in results) / len(results),
        "new_query_calls": len(ids) - len(reused), "reused_queries": len(reused),
        "new_query_tokens": sum(queries[i]["usage"].get("total_tokens", 0) for i in ids if i not in reused),
        "reused_query_tokens": sum(queries[i]["usage"].get("total_tokens", 0) for i in reused),
        "new_embedding_texts": len(inputs), "new_embedding_chunks": new_encoded["encoded_chunks"],
        "historical_vectors_reused": len(set(record_keys.values())),
        "embedding_model": encoded["model"], "embedding_dimensions": encoded["dimensions"],
        "source_records_sha256": digest(source.read_bytes()), "source_memories_unchanged": unchanged,
        "injection_clipped_cases": [r["instance_id"] for r in results if r["injection_clipped"]],
        "incomplete_hit_body_cases": [r["instance_id"] for r in results
                                      if r["both_hit_at_1"] and not all(t["full_target_body_injected"] for t in r["targets"].values())],
        "misses": [r["instance_id"] for r in results if not r["both_hit_at_1"]],
        "coordinator_started": False, "repair_agents_started": False, "harness_started": False,
        "diagnostic_only": True, "same_case_sources_included": True,
    }
    save(args.output / "results.json", results)
    save(args.output / "summary.json", summary)
    lines = ["# 20 题向量召回诊断", "", "只运行查询提取和向量检索，每类 Top-1，不启动修复角色。",
             "检索键仅使用 problem_description 和 usage_scenario；题号只用于检索后核对对应经验。",
             "这是包含同题历史来源的检索诊断，不代表跨题泛化或修复通过率。", "",
             f"全局命中：{summary['global_hit_at_1']}/20；仓库命中：{summary['repository_hit_at_1']}/20；两类同时命中：{summary['both_hit_at_1']}/20。", "",
             "| 问题 | 查询状态 | 全局排名 | 仓库排名/候选数 | 两类命中 |",
             "| --- | --- | ---: | ---: | --- |"]
    for result in results:
        g, r = result["targets"]["global_experience"], result["targets"]["repository_experience"]
        lines.append(f"| [{result['instance_id']}](cases/{result['instance_id']}/coordinator_input.md) | {result['query_status']} | {g['rank']}/20 | {r['rank']}/{r['candidate_count']} | {'是' if result['both_hit_at_1'] else '否'} |")
    lines.extend(["", "[完整结果](results.json) · [汇总](summary.json)", "",
                  "queries/ 保存每题提取输入和输出；cases/ 保存每题完整排名及最终注入文本。",
                  "向量与实际编码文本保存在 query_embeddings.json；历史向量复用前一轮文件，其路径见 experiment.json。"])
    (args.output / "README.md").write_text("\n".join(lines) + "\n")
    save(args.output / "experiment.json", {
        "dataset": str(args.dataset), "previous_result": str(args.previous_result),
        "embedding_model_path": str(args.embedding_model_path), "query_model": model,
        "query_prompt_version": query_module.MEMORY_QUERY_PROMPT_VERSION,
        "query_module_sha256": digest(args.query_module_path.read_bytes()),
        "llm_module_sha256": digest(args.llm_module_path.read_bytes()),
        "limit_per_type": 1, "query_parallelism": 2,
    })
    save(args.output / "progress.json", {"phase": "complete", "completed": 20, "total": 20})
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
