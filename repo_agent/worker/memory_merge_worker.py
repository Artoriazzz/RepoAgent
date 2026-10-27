from __future__ import annotations

import json
import logging
import multiprocessing
import os
import signal
import socket
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from repo_agent.core.llm import DEFAULT_MODEL, ChatLLM
from repo_agent.worker.backend_client import BackendClient, BackendTransportError
from repo_agent.worker.memory_curator import _parse_json_object

LOGGER = logging.getLogger(__name__)


class TextLLM(Protocol):
    def invoke_text(self, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class MemoryMergeDecision:
    action: str
    content: dict[str, Any] | None
    change_reason: str | None


class SemanticMemoryMergeAgent:
    """无状态的 Memory Agent：一次调用只处理一个带租约、带版本的批次。"""

    def __init__(self, llm: TextLLM) -> None:
        self._llm = llm

    def merge(self, batch: dict[str, Any]) -> MemoryMergeDecision:
        prompt = {
            "batch": {
                "operation": batch.get("operation"),
                "scope_type": batch.get("scopeType"),
                "base_version": batch.get("baseVersion"),
                "current_memory": batch.get("currentMemory"),
            },
            "validated_proposals": batch.get("proposals") or [],
        }
        raw = self._llm.invoke_text(_MERGE_SYSTEM_PROMPT, json.dumps(prompt, ensure_ascii=False))
        return self._validate(_parse_json_object(raw))

    @staticmethod
    def _validate(value: dict[str, Any]) -> MemoryMergeDecision:
        action = str(value.get("action") or "").strip().upper()
        if action == "NO_CHANGE":
            return MemoryMergeDecision("NO_CHANGE", None, None)
        if action != "APPLY":
            raise ValueError("Memory Merge Agent returned an invalid action")
        content = value.get("content")
        reason = str(value.get("changeReason") or "").strip()
        required = ("title", "summary", "applicability", "repairStrategy")
        if not isinstance(content, dict) or any(not str(content.get(key) or "").strip() for key in required):
            raise ValueError("Memory Merge Agent returned incomplete semantic content")
        if not reason:
            raise ValueError("Memory Merge Agent did not explain the merged change")
        allowed = {
            "title", "summary", "retrievalText", "applicability", "repairStrategy",
            "verificationStrategy", "antiPatterns", "languageName", "frameworkName",
            "problemType", "dependencyRange",
        }
        limits = {
            "title": 255, "summary": 2_000, "retrievalText": 2_000, "applicability": 4_000,
            "repairStrategy": 4_000, "verificationStrategy": 3_000, "antiPatterns": 3_000,
            "languageName": 64, "frameworkName": 128, "problemType": 128,
            "dependencyRange": 128,
        }
        normalized = {
            key: str(value).strip()[:limits[key]]
            for key, value in content.items()
            if key in allowed and value is not None
        }
        normalized.setdefault("retrievalText", normalized["summary"])
        return MemoryMergeDecision("APPLY", normalized, reason[:4_000])


def run_forever(
    backend: BackendClient,
    agent: SemanticMemoryMergeAgent,
    *,
    worker_id: str,
    lease_seconds: int,
    poll_seconds: float,
) -> None:
    """轮询持久化批次；模型耗时始终在数据库事务和行锁之外。"""
    while True:
        try:
            batch = backend.claim_memory_merge_batch(worker_id, lease_seconds)
            if batch is None:
                time.sleep(poll_seconds)
                continue
            batch_id = UUID(str(batch["batchId"]))
            lease_token = UUID(str(batch["leaseToken"]))
            try:
                decision = agent.merge(batch)
                completed = backend.complete_memory_merge_batch(
                    batch_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    action=decision.action,
                    content=decision.content,
                    change_reason=decision.change_reason,
                )
                LOGGER.info(
                    "Memory merge batch %s completed as %s", batch_id, completed.get("status")
                )
            except Exception as error:
                LOGGER.warning("Memory merge batch %s was abandoned: %s", batch_id, error)
                try:
                    backend.abandon_memory_merge_batch(
                        batch_id,
                        worker_id=worker_id,
                        lease_token=lease_token,
                        reason=str(error)[-2_000:],
                    )
                except Exception as abandon_error:
                    LOGGER.warning("Unable to release memory merge batch %s: %s", batch_id, abandon_error)
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            return
        except BackendTransportError as error:
            LOGGER.warning("Memory merge backend unavailable: %s", error)
            time.sleep(max(1.0, poll_seconds))
        except Exception:
            LOGGER.exception("Memory merge worker loop failed")
            time.sleep(max(1.0, poll_seconds))


def _run_process(
    base_url: str,
    token: str,
    model: str,
    worker_id: str,
    lease_seconds: int,
    poll_seconds: float,
) -> None:
    backend = BackendClient(base_url, token)
    agent = SemanticMemoryMergeAgent(
        ChatLLM(model=model, reasoning_effort="high", enable_thinking=True)
    )
    run_forever(
        backend, agent, worker_id=worker_id, lease_seconds=lease_seconds, poll_seconds=poll_seconds
    )


def main() -> None:
    logging.basicConfig(level=os.getenv("REPO_AGENT_WORKER_LOG_LEVEL", "INFO"))
    memory_mode = os.getenv("REPO_AGENT_MEMORY_MODE", "off").strip().lower()
    if memory_mode not in {"learn", "evolve"}:
        LOGGER.warning(
            "Memory Merge Worker was not started because REPO_AGENT_MEMORY_MODE=%s; "
            "use learn or evolve to publish semantic-memory updates",
            memory_mode,
        )
        return
    base_url = os.getenv("REPO_AGENT_BACKEND_URL")
    token = os.getenv("REPO_AGENT_WORKER_TOKEN")
    if not base_url or not token:
        raise ValueError("REPO_AGENT_BACKEND_URL and REPO_AGENT_WORKER_TOKEN are required")
    concurrency = max(1, int(os.getenv("REPO_AGENT_MEMORY_MERGE_CONCURRENCY", "1")))
    lease_seconds = max(10, int(os.getenv("REPO_AGENT_MEMORY_MERGE_LEASE_SECONDS", "180")))
    poll_seconds = max(0.2, float(os.getenv("REPO_AGENT_MEMORY_MERGE_POLL_SECONDS", "5")))
    model = os.getenv("REPO_AGENT_MEMORY_MERGE_MODEL", os.getenv("REPO_AGENT_MODEL", DEFAULT_MODEL))
    base_worker_id = os.getenv(
        "REPO_AGENT_MEMORY_MERGE_WORKER_ID", f"{socket.gethostname()}-memory-merge"
    )
    if concurrency == 1:
        _run_process(base_url, token, model, base_worker_id, lease_seconds, poll_seconds)
        return

    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_run_process,
            args=(base_url, token, model, f"{base_worker_id}-{index + 1}", lease_seconds, poll_seconds),
            name=f"repo-agent-memory-merge-{index + 1}",
        )
        for index in range(concurrency)
    ]

    def stop_workers(_signum=None, _frame=None) -> None:
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, stop_workers)
    try:
        for process in processes:
            process.start()
        while True:
            for process in processes:
                process.join(timeout=1)
                if process.exitcode is not None:
                    raise RuntimeError(f"{process.name} stopped with exit code {process.exitcode}")
    except KeyboardInterrupt:
        LOGGER.info("Stopping Memory Merge workers")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)
        signal.signal(signal.SIGTERM, previous_sigterm)


_MERGE_SYSTEM_PROMPT = """你负责审查并合并一条待写入的长期修复经验。
你没有其他会话或调用方架构的隐含上下文，只能使用本提示、当前提案和已有经验。
输入是同一记忆正式版本上的若干、已独立验证成功的修复经验提案。你的职责是把真正可复用
的新信息合并进一条语义记忆，不是拼接原始 patch，也不是记录题目答案。

规则：
1. 当前记忆为空时，只有提案能形成可迁移策略才 APPLY；否则 NO_CHANGE。
2. 当前记忆不为空时，只补充新的适用边界、抽象修复步骤、公开验证方式或已证伪反模式；
   不要用较窄案例替换原有通用策略。
3. 禁止输出 issue 编号、实例 ID、仓库私有文件路径、完整 diff、隐藏测试名、gold patch 和具体答案。
4. retrievalText 仅描述召回对象、操作、故障特征、预期契约和适用条件；不能写具体实现细节。
5. 不能确认有新增价值时必须 NO_CHANGE。不要输出 markdown。

严格输出 JSON：
{"action":"APPLY|NO_CHANGE","content":{"title":"","summary":"","retrievalText":"",
"applicability":"","repairStrategy":"","verificationStrategy":"","antiPatterns":"",
"languageName":"","frameworkName":"","problemType":"","dependencyRange":""},"changeReason":""}
NO_CHANGE 不需要 content 或 changeReason。"""


if __name__ == "__main__":
    main()
