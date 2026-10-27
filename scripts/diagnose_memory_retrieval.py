"""只执行独立查询提取、经验检索和注入文本格式化，不启动修复角色。"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace


def vector_rankings(records, query_text, *, output, python, model_path, cached_embeddings=None):
    inputs = {"query": {"id": "query", "text": query_text, "is_query": True}}
    record_keys = {}
    for record in records:
        text = f"问题描述：{record['problem_description']}\n使用场景：{record['usage_scenario']}"
        key = hashlib.sha256(text.encode()).hexdigest()
        record_keys[record["memory_filename"]] = key
        inputs[key] = {"id": key, "text": text, "is_query": False}
    input_path = output / "embedding_input.json"
    input_path.write_text(json.dumps(list(inputs.values()), ensure_ascii=False, indent=2))
    if cached_embeddings is not None:
        if json.loads(cached_embeddings.with_name("embedding_input.json").read_text()) != list(inputs.values()):
            raise ValueError("Cached vectors were produced for different retrieval texts")
        shutil.copy2(cached_embeddings, output / "embeddings.json")
    else:
        subprocess.run([
            str(python), str(Path(__file__).with_name("embed_retrieval_texts.py")),
            "--input", str(input_path), "--output", str(output / "embeddings.json"),
            "--model-path", str(model_path),
        ], check=True)
    encoded = json.loads((output / "embeddings.json").read_text())
    vectors = encoded["vectors"]
    # 长文本按块计算余弦，取最高块间分数；同一经验对的相同检索文本只编码一次。
    scores = {
        filename: max(sum(a * b for a, b in zip(query, document))
                      for query in vectors["query"] for document in vectors[key])
        for filename, key in record_keys.items()
    }
    return scores, {key: value for key, value in encoded.items() if key not in {"vectors", "encoded_texts"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--query-module-path", type=Path)
    parser.add_argument("--llm-module-path", type=Path)
    parser.add_argument("--retriever", choices=("bm25", "vector"), default="bm25")
    parser.add_argument("--cached-query-file", type=Path)
    parser.add_argument("--embedding-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--embedding-model-path", type=Path)
    parser.add_argument("--cached-embeddings-file", type=Path)
    args = parser.parse_args()
    if args.retriever == "vector" and not (args.embedding_model_path or args.cached_embeddings_file):
        parser.error("--embedding-model-path is required for vector retrieval")
    if args.env_file:
        for line in args.env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip().strip("'\"")

    if args.llm_module_path:
        spec = importlib.util.spec_from_file_location("repo_agent.core.llm", args.llm_module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

    from repo_agent.core.experience import augment_coordinator_with_experiences
    from repo_agent.core.llm import ChatLLM
    from repo_agent.core.memory import MemoryStore, MemoryType
    from repo_agent.core.semantic_evidence import extract_public_problem_statement

    # 允许在远程独立目录验证新提取器，避免覆盖正在协作的代码或冻结实验。
    if args.cached_query_file:
        agent_type = None
    elif args.query_module_path:
        spec = importlib.util.spec_from_file_location("retrieval_query_diagnostic", args.query_module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        agent_type = module.MemoryQueryAgent
    else:
        from repo_agent.core.memory_query import MemoryQueryAgent
        agent_type = MemoryQueryAgent

    records = json.loads((args.dataset / "retrieval_records.json").read_text())["records"]
    source_hashes = {
        r["memory_filename"]: hashlib.sha256((args.dataset / "memories" / r["memory_filename"]).read_bytes()).hexdigest()
        for r in records
    }
    target = [r for r in records if r["source_case_id"] == args.instance_id]
    if len(target) != 2:
        raise ValueError("Expected one global and one repository target memory")
    repository = target[0]["repository"]
    candidates = [r for r in records if r["memory_type"] == "global_experience" or r["repository"] == repository]
    args.output.mkdir(parents=True, exist_ok=False)
    store = MemoryStore.for_scope(repository, root=args.output / "memory")
    for record in candidates:
        directory = store.global_memory_dir if record["memory_type"] == "global_experience" else store.memory_dir
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.dataset / "memories" / record["memory_filename"], directory / record["memory_filename"])
    store.update_index()
    task = (args.dataset / "source_tasks" / f"{args.instance_id}.md").read_text()
    public_issue = extract_public_problem_statement(task)
    if args.cached_query_file:
        request_path = args.cached_query_file.with_name("request.json")
        request = json.loads(request_path.read_text())
        if json.loads(request["messages"][1]["content"])["issue_body"] != public_issue:
            raise ValueError("Cached extraction was produced for a different public Issue")
        saved = json.loads(args.cached_query_file.read_text())
        result = SimpleNamespace(status=saved["status"], retrieval_text=saved["retrieval_text"])
        query_dir = args.output / "memory-query"
        query_dir.mkdir()
        shutil.copy2(args.cached_query_file, query_dir / "result.json")
        shutil.copy2(request_path, query_dir / "request.json")
    else:
        llm = ChatLLM(
            model=args.model or os.getenv("REPO_AGENT_MEMORY_QUERY_MODEL") or os.getenv("OPENAI_MODEL"),
            api_key=os.getenv("REPO_AGENT_MEMORY_QUERY_API_KEY") or os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("REPO_AGENT_MEMORY_QUERY_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
            enable_thinking=False, reasoning_effort="low", max_output_tokens=4096,
            output_limit_recovery=False,
        )
        result = agent_type(llm).extract(issue_body=public_issue, output_dir=args.output / "memory-query")

    scores, embedding = {}, None
    if args.retriever == "vector":
        scores, embedding = vector_rankings(candidates, result.retrieval_text, output=args.output,
                                           python=args.embedding_python, model_path=args.embedding_model_path,
                                           cached_embeddings=args.cached_embeddings_file)
    rankings = {}
    for kind in (MemoryType.GLOBAL_EXPERIENCE, MemoryType.REPOSITORY_EXPERIENCE):
        if args.retriever == "vector":
            from repo_agent.core.memory import RecalledMemory
            headers = [header for header in store.list_memories() if header.type == kind]
            headers.sort(key=lambda h: (-scores[h.filename], h.filename))
            rankings[kind.value] = [RecalledMemory(header=h, content=store.read_memory(h.filename)) for h in headers]
        else:
            rankings[kind.value] = store.recall_experiences(
                result.retrieval_text, memory_type=kind, limit=len(candidates),
            )

    class RankedStore:
        def recall_experiences(self, query, *, memory_type, limit):
            return rankings[memory_type.value][:limit]

    # 复用正式的格式化与长度限制，只生成输入文件，不实例化 Coordinator。
    text, selected = augment_coordinator_with_experiences(
        task=task, memory_store=RankedStore(), limit_per_type=1,
    )
    (args.output / "coordinator_input.md").write_text(text)
    (args.output / "injected_experiences.md").write_text(text[:-len(task)] if text.endswith(task) else text)
    rows = {r["memory_filename"]: r for r in records}
    report = {
        "instance_id": args.instance_id, "repository": repository,
        "retriever": "vector_problem_description_usage_scenario" if args.retriever == "vector"
                     else "existing_bm25_name_description_full_memory",
        "candidate_counts": {kind.value: sum(r["memory_type"] == kind.value for r in candidates)
                             for kind in (MemoryType.GLOBAL_EXPERIENCE, MemoryType.REPOSITORY_EXPERIENCE)},
        "query_status": result.status, "query": result.retrieval_text,
        "rankings": {kind: [{"rank": i, "filename": memory.header.filename,
                              "source_case_id": rows[memory.header.filename]["source_case_id"],
                              **({"cosine_similarity": scores[memory.header.filename]} if scores else {})}
                             for i, memory in enumerate(memories, 1)] for kind, memories in rankings.items()},
        "expected_memories": [{
            "filename": r["memory_filename"],
            "rank": next((i for i, m in enumerate(rankings[r["memory_type"]], 1)
                          if m.header.filename == r["memory_filename"]), None),
            "selected": any(m.header.filename == r["memory_filename"] for m in selected),
            "original_body_fully_injected": r["original_memory"]["content"].strip() in text,
        } for r in target],
        "selected_memories": [m.header.filename for m in selected],
        "coordinator_started": False, "repair_agents_started": False,
        "embedding_computed": args.retriever == "vector" and not args.cached_embeddings_file,
        "embedding_reused": bool(args.cached_embeddings_file), "embedding": embedding,
        "limit_per_type": 1,
        "query_reused": bool(args.cached_query_file),
        "new_llm_calls": 0 if args.cached_query_file else 1,
        "injection_clipped": "[... clipped " in text,
        "source_memories_unchanged": all(
            hashlib.sha256((args.dataset / "memories" / filename).read_bytes()).hexdigest() == digest
            for filename, digest in source_hashes.items()
        ),
    }
    (args.output / "retrieval_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
