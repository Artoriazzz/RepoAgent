"""用固定历史请求比较直连/代理和流式/非流式；不执行任何模型工具。"""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import httpx


def main():
    from swebench_batch import read_env_file
    from repo_agent.core.coordinator import CoordinatorEngine
    from repo_agent.core.llm import ChatLLM
    from repo_agent.core.memory import MemoryStore
    from repo_agent.tools import RepoWorkspace, ToolImportContext, assemble_tool_pool

    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    for key, value in read_env_file(project / ".env").items():
        os.environ.setdefault(key, value)
    args.output.mkdir(parents=True, exist_ok=False)
    llm = ChatLLM(model="glm-5.3-flash", reasoning_effort="high")
    store = MemoryStore.for_project(args.repo)
    registry = assemble_tool_pool(ToolImportContext(
        workspace=RepoWorkspace(repo_path=args.repo, output_dir=args.output / "schema"),
        memory_store=store, enable_github=False, enable_memory=True))
    coordinator = CoordinatorEngine(llm=llm, worker_tools=registry, cwd=args.repo,
        output_dir=args.output / "schema", memory_store=store, swebench_profile=True,
        repair_mode="experience-build", repair_memory_recall=False)
    payload = {"model": llm.model, "messages": json.loads(args.messages.read_text()),
               "tools": coordinator.engine.tools.schemas(), "tool_choice": "auto",
               **llm._completion_options()}
    base_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    (args.output / "request.json").write_bytes(base_bytes)
    manifest = {"request_sha256": hashlib.sha256(base_bytes).hexdigest(),
                "message_count": len(payload["messages"]),
                "message_chars": len(json.dumps(payload["messages"], ensure_ascii=False)),
                "tool_schema_chars": len(json.dumps(payload["tools"], ensure_ascii=False)),
                "read_timeout_seconds": 300, "total_budget_seconds": 600, "retries": 0}
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    llm.client.close()
    endpoint = (llm.base_url or "").rstrip("/") + "/chat/completions"
    results = []
    # 顺序执行以避免并发限流干扰；单轮结果仅用于诊断，不代表统计性能。
    for route, stream in [("direct", False), ("proxy", False), ("direct", True), ("proxy", True)]:
        label = route + ("-stream" if stream else "-nonstream")
        result = {"label": label, "route": route, "stream": stream}
        start = time.monotonic()
        print(f"START {label}", flush=True)
        body = {**payload, "stream": stream}
        if stream:
            body["stream_options"] = {"include_usage": True}
        chunks = []
        try:
            with httpx.Client(proxy=args.proxy if route == "proxy" else None, trust_env=False,
                              timeout=httpx.Timeout(300, connect=15, write=30, pool=15)) as client:
                with client.stream("POST", endpoint, json=body,
                    headers={"Authorization": "Bearer " + llm.api_key}) as response:
                    result.update(status_code=response.status_code,
                                  headers_seconds=round(time.monotonic() - start, 3))
                    response.raise_for_status()
                    if not stream:
                        data = json.loads(response.read())
                        result["usage"] = data.get("usage")
                        result["finish_reasons"] = [x.get("finish_reason") for x in data.get("choices", [])]
                        chunks.append(data)
                    else:
                        done = False
                        for line in response.iter_lines():
                            if time.monotonic() - start > 600:
                                raise TimeoutError("Streaming total budget exceeded")
                            if not line.startswith("data:"):
                                continue
                            result.setdefault("first_event_seconds", round(time.monotonic() - start, 3))
                            content = line[5:].strip()
                            if content == "[DONE]":
                                done = True
                                break
                            chunk = json.loads(content)
                            chunks.append(chunk)
                            if chunk.get("usage"):
                                result["usage"] = chunk["usage"]
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta", {})
                                if any(delta.get(k) for k in ["content", "reasoning_content", "tool_calls"]):
                                    result.setdefault("first_output_seconds", round(time.monotonic() - start, 3))
                        if not done:
                            raise RuntimeError("Stream ended without DONE")
                        result["finish_reasons"] = [c["finish_reason"] for x in chunks
                            for c in x.get("choices", []) if c.get("finish_reason")]
                    result["status"] = "ok"
        except Exception as exc:
            result.update(status="error", error_type=type(exc).__name__)
        result["seconds"] = round(time.monotonic() - start, 3)
        (args.output / (label + "-response.json")).write_text(json.dumps(chunks, ensure_ascii=False))
        results.append(result)
        (args.output / "results.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(result), flush=True)
    print("FINISHED", flush=True)


if __name__ == "__main__":
    main()
